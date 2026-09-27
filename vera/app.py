"""Vera chatbot — HTTP API.

Author: Pranav Yadav (B.Tech Mathematics & Computing, DTU)

POST /v1/context   POST /v1/tick   POST /v1/reply   GET /v1/healthz   GET /v1/metadata   (+ POST /v1/teardown)
"""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait as fwait
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import llm
from .composer import compose
from .replies import handle_reply
from .store import SCOPES, STORE, Conversation
from .util import merchant_prefers_hinglish, parse_dt

app = FastAPI(title="Vera bot", version="1.0.0")

from .playground import router as _playground_router  # noqa: E402
app.include_router(_playground_router)

MAX_ACTIONS = 20
TICK_BUDGET_S = float(os.getenv("TICK_BUDGET_S", "18"))
_pool = ThreadPoolExecutor(max_workers=8)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _json(req: Request):
    try:
        data = await req.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ------------------------------------------------------------------ health / metadata
@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - STORE.started), "contexts_loaded": STORE.counts()}


@app.get("/v1/metadata")
async def metadata():
    members = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Pranav Yadav").split(",") if m.strip()]
    return {
        "team_name": os.getenv("TEAM_NAME", "Pranav Yadav (DTU)"),
        "team_members": members,
        "model": llm.model_label(),
        "approach": ("Event-type router over a 4-part context composer: dedicated handlers ground every number in "
                     "stored data (no made-up facts); urgency-ranked scheduling with consent, de-duplication and "
                     "per-business pacing; conversation engine with auto-reply detection, yes->action switch, "
                     "hostile/off-topic handling, per-turn Hinglish detection and a polite goodbye; optional "
                     "validated LLM polish."),
        "contact_email": os.getenv("CONTACT_EMAIL", "rishabhpranav26@gmail.com"),
        "version": "1.1.0",
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-27T08:00:00Z"),
    }


# ------------------------------------------------------------------ context
@app.post("/v1/context")
async def push_context(req: Request):
    b = await _json(req)
    if b is None:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_json", "details": "body must be a JSON object"})
    scope, cid, ver, payload = b.get("scope"), b.get("context_id"), b.get("version"), b.get("payload")
    if scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"})
    if not isinstance(cid, str) or not cid:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_context_id", "details": "context_id required"})
    try:
        ver = int(ver)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_version", "details": "version must be an integer"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_payload", "details": "payload must be an object"})
    ok, cur = STORE.put(scope, cid, ver, payload)
    if not ok:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": cur})
    return {"accepted": True, "ack_id": f"ack_{cid}_v{ver}", "stored_at": _now_iso()}


# ------------------------------------------------------------------ tick
def _build_action(trg: dict, now: datetime) -> dict | None:
    mid = trg.get("merchant_id")
    merchant = STORE.get("merchant", mid)
    if not merchant:
        return None
    category = STORE.get("category", merchant.get("category_slug")) or {}
    customer = STORE.get("customer", trg.get("customer_id")) if trg.get("customer_id") else None
    if trg.get("customer_id") and not customer:
        return None  # customer-scoped trigger without customer context: don't guess
    c = compose(category, merchant, trg, customer, now)
    if c.get("_skip"):
        return None
    if llm.enabled():
        facts = {"merchant": merchant, "trigger": trg, "customer": customer,
                 "category": {k: category.get(k) for k in ("slug", "peer_stats", "digest", "offer_catalog", "seasonal_beats")}}
        better = llm.polish(c["body"], facts, category.get("voice") or {}, merchant_prefers_hinglish(merchant),
                            (category.get("voice") or {}).get("vocab_taboo") or [])
        if better:
            c["body"] = better
            c["rationale"] += " (LLM-polished; grounding validated)"
    return c


@app.post("/v1/tick")
async def tick(req: Request):
    b = await _json(req) or {}
    now = parse_dt(b.get("now")) or datetime.now(timezone.utc)
    ids = b.get("available_triggers") or []
    if not isinstance(ids, list):
        ids = []

    # 1. resolve + filter
    cands = []
    seen = set()
    for tid in ids:
        trg = STORE.get("trigger", tid)
        if not trg:
            continue
        real_id = trg.get("id") or tid
        if real_id in seen:
            continue
        seen.add(real_id)
        sk = trg.get("suppression_key")
        if real_id in STORE.sent_triggers or (sk and sk in STORE.used_suppression):
            continue
        mid = trg.get("merchant_id")
        fl = STORE.flag(mid)
        is_cust = bool(trg.get("customer_id"))
        if not is_cust:
            if fl.opted_out:
                continue
            bu = parse_dt(fl.backoff_until)
            if bu and bu > now and (trg.get("urgency") or 0) < 5:
                continue
        cands.append((tid, trg))

    # 2. prioritise: urgency desc; one merchant-facing message per merchant per tick; one per customer
    cands.sort(key=lambda x: (-(x[1].get("urgency") or 0), x[1].get("expires_at") or ""))
    chosen, used_keys = [], set()
    for tid, trg in cands:
        key = ("c", trg.get("customer_id")) if trg.get("customer_id") else ("m", trg.get("merchant_id"))
        if key in used_keys:
            continue
        used_keys.add(key)
        chosen.append((tid, trg))
        if len(chosen) >= MAX_ACTIONS:
            break

    # 3. compose (parallel, with a hard time budget)
    futs = {_pool.submit(_build_action, trg, now): (tid, trg) for tid, trg in chosen}
    done, _ = fwait(futs, timeout=TICK_BUDGET_S)
    actions = []
    for f in futs:
        if f not in done:
            continue
        try:
            c = f.result()
        except Exception:
            c = None
        if not c:
            continue
        tid, trg = futs[f]
        real_id = trg.get("id") or tid
        mid, cust = trg.get("merchant_id"), trg.get("customer_id")
        conv_id = f"conv_{(cust or mid)}_{real_id}"
        if conv_id in STORE.convs:
            conv_id += "_" + uuid.uuid4().hex[:6]
        with STORE.lock:
            STORE.sent_triggers.add(real_id)
            if trg.get("suppression_key"):
                STORE.used_suppression.add(trg["suppression_key"])
            tp = c["template_params"]
            STORE.convs[conv_id] = Conversation(
                conversation_id=conv_id, merchant_id=mid, customer_id=cust, trigger_id=real_id,
                kind=trg.get("kind", "generic"), send_as=c["send_as"],
                topic=(tp[1] if len(tp) > 1 else "") or trg.get("kind", ""),
                offer=c["_offer"], deliverable=c["_deliverable"], bot_bodies=[c["body"]])
            STORE.flag(mid).last_sent_at = now.isoformat()
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": mid,
            "customer_id": cust,
            "send_as": c["send_as"],
            "trigger_id": real_id,
            "template_name": c["template_name"],
            "template_params": c["template_params"],
            "body": c["body"],
            "cta": c["cta"],
            "suppression_key": c["suppression_key"],
            "rationale": c["rationale"],
        })
    return {"actions": actions}


# ------------------------------------------------------------------ reply
@app.post("/v1/reply")
async def reply(req: Request):
    b = await _json(req)
    if b is None or not b.get("conversation_id"):
        return JSONResponse(status_code=400, content={"action": "end", "rationale": "malformed reply payload"})
    try:
        return handle_reply(b)
    except Exception as e:  # never return a 500 to the caller
        return {"action": "wait", "wait_seconds": 1800, "rationale": f"Internal issue handling this turn ({type(e).__name__}); backing off briefly."}


@app.post("/v1/teardown")
async def teardown():
    STORE.reset()
    return {"ok": True, "wiped_at": _now_iso()}
