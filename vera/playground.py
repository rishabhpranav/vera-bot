"""Interactive playground: type a merchant/customer reply, see what the bot does.

Runs on its own Store (preloaded with the bundled sample data) so it never touches
contexts or conversations pushed through the /v1 API.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .composer import compose
from .replies import handle_reply
from .store import Conversation, Store, reset_store, use_store

DATA = Path(__file__).resolve().parent.parent / "data"
router = APIRouter()
_lock = threading.Lock()
_play: Store | None = None


def _load() -> Store:
    st = Store()
    for scope, sub, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                            ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
        for f in sorted((DATA / sub).glob("*.json")):
            p = json.loads(f.read_text())
            st.put(scope, p.get(key) or f.stem, 1, p)
    return st


def play() -> Store:
    global _play
    with _lock:
        if _play is None:
            _play = _load()
        return _play


@router.get("/playground/api/catalog")
async def catalog():
    st = play()
    merchants, triggers = [], {}
    for (scope, cid), e in st.contexts.items():
        p = e["payload"]
        if scope == "merchant":
            idn = p.get("identity", {})
            merchants.append({"id": cid, "name": idn.get("name"), "category": p.get("category_slug"),
                              "city": f"{idn.get('locality', '')}, {idn.get('city', '')}".strip(", ")})
        elif scope == "trigger":
            cust = st.get("customer", p.get("customer_id")) if p.get("customer_id") else None
            triggers.setdefault(p.get("merchant_id"), []).append({
                "id": p.get("id") or cid, "kind": p.get("kind"), "urgency": p.get("urgency"),
                "customer": (cust or {}).get("identity", {}).get("name") if cust else None})
    merchants.sort(key=lambda m: m["id"])
    for v in triggers.values():
        v.sort(key=lambda t: -(t["urgency"] or 0))
    return {"merchants": merchants, "triggers": triggers}


@router.post("/playground/api/start")
async def start(req: Request):
    b = await req.json()
    st = play()
    mid, tid = b.get("merchant_id"), b.get("trigger_id")
    merchant = st.get("merchant", mid)
    if not merchant:
        return JSONResponse(status_code=400, content={"error": "unknown merchant"})
    conv_id = f"play_{uuid.uuid4().hex[:8]}"
    if not tid:  # free chat: merchant speaks first
        tok = use_store(st)
        try:
            from .replies import ensure_conv
            ensure_conv({"conversation_id": conv_id, "merchant_id": mid})
        finally:
            reset_store(tok)
        return {"conversation_id": conv_id, "opening": None}
    trg = st.get("trigger", tid)
    category = st.get("category", merchant.get("category_slug")) or {}
    customer = st.get("customer", trg.get("customer_id")) if trg.get("customer_id") else None
    c = compose(category, merchant, trg, customer, datetime.now(timezone.utc))
    tp = c["template_params"]
    with st.lock:
        st.convs[conv_id] = Conversation(
            conversation_id=conv_id, merchant_id=mid, customer_id=trg.get("customer_id"), trigger_id=tid,
            kind=trg.get("kind", "generic"), send_as=c["send_as"], topic=(tp[1] if len(tp) > 1 else ""),
            offer=c["_offer"], deliverable=c["_deliverable"], bot_bodies=[c["body"]])
    return {"conversation_id": conv_id,
            "opening": {k: c[k] for k in ("body", "cta", "send_as", "rationale", "template_name", "suppression_key")},
            "skipped_reason": c.get("_skip"), "reply_role": "customer" if trg.get("customer_id") else "merchant"}


@router.post("/playground/api/reply")
async def reply(req: Request):
    b = await req.json()
    st = play()
    conv = st.convs.get(b.get("conversation_id"))
    if not conv:
        return JSONResponse(status_code=400, content={"error": "start a conversation first"})
    tok = use_store(st)
    try:
        out = handle_reply({"conversation_id": conv.conversation_id, "merchant_id": conv.merchant_id,
                             "customer_id": conv.customer_id,
                             "from_role": "customer" if conv.customer_id else "merchant",
                             "message": b.get("message", ""),
                             "received_at": datetime.now(timezone.utc).isoformat(),
                             "turn_number": len(conv.inbound) + 2})
        out["conversation_closed"] = conv.ended
        return out
    finally:
        reset_store(tok)


@router.post("/playground/api/reset")
async def reset():
    global _play
    with _lock:
        _play = None
    play()
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
@router.get("/playground", response_class=HTMLResponse)
async def page():
    return HTMLResponse((Path(__file__).parent / "playground.html").read_text())
