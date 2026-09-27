"""Reply engine: classify the inbound turn, then route to send / wait / end."""
from __future__ import annotations

import re
from datetime import timedelta

from . import llm
from .composer import compose
from .store import S, Conversation
from .util import is_hinglish_text, merchant_prefers_hinglish, parse_dt, humanize, nice_date

# ------------------------------------------------------------------ patterns
OPTOUT = re.compile(
    r"\b(stop|unsubscribe|not interested|no interest|don'?t (?:message|msg|text|contact|call)|do not (?:message|contact|text)|"
    r"leave me alone|remove me|block(?:ing)? you|band karo|mat bhejo|message mat|msg mat|nahi chahiye|nai chahiye|"
    r"no more messages|stop sending|stop messaging)\b", re.I)
HOSTILE = re.compile(
    r"\b(useless|spam|spammer|bothering|irritat\w*|nonsense|bakwas|bakwaas|pagal|idiot|stupid|shut up|fraud|scam|"
    r"waste of (?:my )?time|bekaar|bekar|annoying|harass\w*|chutiya|bc|mc|bloody|damn|hell|f+u+c+k\w*|shit|bullshit|"
    r"get lost|why are you (?:bothering|messaging|disturbing))\b", re.I)
AUTO = re.compile(
    r"(thank(?:s| you) for (?:contacting|reaching|your message|messaging|writing)|we will (?:get back|respond|revert|contact)|"
    r"will (?:get back|respond|revert) (?:to you )?(?:shortly|soon|asap)|our team will|team will (?:respond|reply|get back)|"
    r"automated (?:message|response|reply|assistant)|auto[- ]?reply|out of (?:the )?office|currently (?:unavailable|closed|away)|"
    r"business hours|working hours are|aapki jaankari ke liye|team tak pahuncha|hamari team|sampark karne ke liye|"
    r"i am an automated|this is an automated|we are closed|will be in touch)", re.I)
COMMIT = re.compile(
    r"\b(yes|yeah|yea|yep|yup|haan|haa|han|haanji|hanji|ok(?:ay)?|okk+|sure|go ahead|let'?s do it|lets do it|do it|"
    r"let'?s go|proceed|confirm(?:ed)?|please do|pls do|send (?:it|me|the|now)|kar do|kardo|kar dijiye|karo|chalo|chalega|"
    r"theek hai|thik hai|sounds good|perfect|go for it|i want to join|judna hai|join karna|sign me up|book(?: it)?|"
    r"interested|i'?m in|approved|publish|schedule it|done)\b", re.I)
DEFER = re.compile(
    r"\b(later|busy|baad mein|baad me|kal|tomorrow|in a meeting|not now|abhi nahi|next week|thodi der|call me|"
    r"after some time|remind me|give me (?:some )?time)\b", re.I)
OFFTOPIC = re.compile(
    r"\b(gst|income tax|itr|tax filing|tax return|loan|insurance|electricity|passport|visa|aadhaar|aadhar|pan card|"
    r"legal notice|lawyer|accounting|accountant|salary|payroll|cricket score|stock tips?|share market|mutual fund|"
    r"bank account|credit card|website (?:banana|development)|app development|instagram followers)\b", re.I)
STRONG_COMMIT = re.compile(
    r"\b(yes|yeah|yep|yup|haan|haanji|hanji|go ahead|let'?s do it|lets do it|do it|let'?s go|proceed|confirm(?:ed)?|"
    r"please do|pls do|send (?:it|me|the|now)|kar do|kardo|kar dijiye|chalo|go for it|i want to join|judna hai|"
    r"join karna|sign me up|i'?m in|approved|publish|schedule it)\b", re.I)
CHANGE_REQ = re.compile(r"\b(instead|change|make it|can we (?:make|do|add|change)|modify|edit|replace|add (?:a|an|one))\b", re.I)
NEGATIVE = re.compile(r"^\s*(no|nope|nah|nahi|nahin|na|not really|no thanks|no thank you|not required|pass)\b", re.I)
QUESTION = re.compile(
    r"(\?|^\s*(what|how|why|when|where|which|who|can|could|does|do|is|are|will|kitna|kitne|kya|kaise|kab|kaun|kahan)\b|"
    r"\b(price|cost|charges?|fees?|kitna|details|explain|tell me more|batao|bataiye)\b)", re.I)
PRICE_Q = re.compile(r"\b(price|cost|charges?|fees?|kitna|kitne|how much|amount|paisa|paise|rate)\b", re.I)
SLOT_PICK = re.compile(r"^\s*([1-3])\b")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def classify(msg: str, conv: Conversation, flags) -> str:
    m = (msg or "").strip()
    if not m:
        return "empty"
    n = _norm(m)
    if OPTOUT.search(m) and not re.search(r"\bdon'?t stop\b", m, re.I):
        return "optout"
    prev = [_norm(x) for x in conv.inbound[:-1]]
    if AUTO.search(m) or (len(n) > 25 and (n in prev or n in flags.auto_texts)):
        return "auto"
    if HOSTILE.search(m):
        return "hostile"
    if conv.customer_id and SLOT_PICK.match(m):
        return "commit"
    if OFFTOPIC.search(m):
        return "offtopic"
    if DEFER.search(m):
        return "defer"
    if CHANGE_REQ.search(m) and conv.stage in ("pitched", "drafted") and not conv.customer_id:
        return "change"
    if STRONG_COMMIT.search(m) and not NEGATIVE.match(m):
        return "commit"
    if COMMIT.search(m) and not NEGATIVE.match(m) and not QUESTION.search(m):
        return "commit"
    if NEGATIVE.match(m):
        return "negative"
    if QUESTION.search(m):
        return "question"
    return "other"


# ------------------------------------------------------------------ helpers
def _ctx(conv: Conversation):
    merchant = S().get("merchant", conv.merchant_id) or {"merchant_id": conv.merchant_id, "identity": {}}
    category = S().get("category", merchant.get("category_slug")) or {}
    trigger = S().get("trigger", conv.trigger_id) or {}
    customer = S().get("customer", conv.customer_id) if conv.customer_id else None
    return category, merchant, trigger, customer


EN_WORDS = {"the", "and", "please", "what", "how", "is", "are", "you", "your", "can", "will", "this", "that",
            "send", "me", "my", "we", "our", "it", "for", "with", "do", "does", "not", "want", "need", "about",
            "i", "to", "a", "hi", "hello", "have", "would", "like", "let"}


def _hi(conv: Conversation, msg: str, merchant: dict) -> bool:
    if is_hinglish_text(msg):
        conv.lang_hinglish = True
        return True
    words = re.findall(r"[a-z]+", (msg or "").lower())
    if sum(1 for w in words if w in EN_WORDS) >= 2:
        conv.lang_hinglish = False
        return False
    if conv.lang_hinglish is not None:
        return conv.lang_hinglish
    if conv.customer_id:
        cust = S().get("customer", conv.customer_id) or {}
        return str((cust.get("identity") or {}).get("language_pref", "")).lower().startswith("hi")
    return merchant_prefers_hinglish(merchant)


def _send(conv: Conversation, body: str, cta: str, rationale: str) -> dict:
    body = re.sub(r"https?://\S+", "", body).strip()
    if body in conv.bot_bodies:  # anti-repetition
        body = body + (" (Reply STOP anytime to pause these.)" if "STOP" not in body else " 🙏")
        if body in conv.bot_bodies:
            conv.ended = True
            return {"action": "end", "rationale": "Would otherwise repeat an earlier message verbatim; closing instead."}
    conv.bot_bodies.append(body)
    return {"action": "send", "body": body, "cta": cta, "rationale": rationale}


KIND_TOPIC = {
    "research_digest": "the research item", "regulation_change": "the compliance deadline", "cde_opportunity": "the webinar",
    "perf_dip": "the drop in your numbers", "seasonal_perf_dip": "the seasonal dip", "perf_spike": "your recent spike",
    "renewal_due": "your renewal", "festival_upcoming": "the festival plan", "curious_ask_due": "this week's demand",
    "winback_eligible": "restarting your listing", "dormant_with_vera": "your listing", "ipl_match_today": "tonight's match",
    "review_theme_emerged": "your reviews", "milestone_reached": "your milestone", "active_planning_intent": "your plan",
    "supply_alert": "the recall", "category_seasonal": "the seasonal demand shift", "gbp_unverified": "verification",
    "competitor_opened": "the new competitor",
}
PUBLISH_KINDS = {"perf_dip", "perf_spike", "festival_upcoming", "ipl_match_today", "active_planning_intent", "competitor_opened",
                 "milestone_reached", "review_theme_emerged", "dormant_with_vera", "curious_ask_due", "seasonal_perf_dip"}


def _topic(conv: Conversation) -> str:
    t = KIND_TOPIC.get(conv.kind) or conv.topic or humanize(conv.kind)
    if len(t) > 70:
        t = t[:70].rsplit(" ", 1)[0] + "…"
    return t


def _backoff(conv: Conversation, received_at: str | None, seconds: int):
    base = parse_dt(received_at)
    if base and conv.merchant_id:
        S().flag(conv.merchant_id).backoff_until = (base + timedelta(seconds=seconds)).isoformat()


def ensure_conv(body: dict) -> Conversation:
    cid = body.get("conversation_id") or "conv_unknown"
    with S().lock:
        conv = S().convs.get(cid)
        if conv:
            return conv
        mid = body.get("merchant_id")
        cust = body.get("customer_id")
        # Unknown conversation (e.g. a reply to a conversation this instance didn't start): attach the merchant's most urgent stored trigger.
        trig = None
        if mid:
            ts = [t for t in S().triggers_for_merchant(mid) if bool(t.get("customer_id")) == bool(cust)]
            ts.sort(key=lambda t: -(t.get("urgency") or 0))
            trig = ts[0] if ts else None
        conv = Conversation(conversation_id=cid, merchant_id=mid, customer_id=cust,
                            trigger_id=trig.get("id") if trig else None,
                            kind=trig.get("kind") if trig else "generic",
                            send_as="merchant_on_behalf" if cust else "vera")
        merchant = S().get("merchant", mid)
        if merchant:
            category = S().get("category", merchant.get("category_slug")) or {}
            c = compose(category, merchant, trig or {"kind": "generic", "payload": {}}, S().get("customer", cust))
            conv.offer, conv.deliverable = c["_offer"], c["_deliverable"]
            conv.topic = (c["template_params"][1] if len(c["template_params"]) > 1 else "") or humanize(conv.kind)
        else:
            conv.offer = "draft this week's Google post"
            conv.deliverable = ("Plan: 1) Refresh your Google profile photos and hours; 2) Publish 2 posts this week "
                                "(I'll draft both); 3) Send a review request to recent customers (template ready).")
        S().convs[cid] = conv
        return conv


# ------------------------------------------------------------------ main entry
def _handle_reply_inner(body: dict) -> dict:
    conv = ensure_conv(body)
    msg = body.get("message") or ""
    with S().lock:
        conv.inbound.append(msg)
    flags = S().flag(conv.merchant_id)
    if conv.ended:
        return {"action": "end", "rationale": "Conversation already closed (goodbye already sent); not re-engaging."}
    category, merchant, trigger, customer = _ctx(conv)
    hi = _hi(conv, msg, merchant)
    kind = classify(msg, conv, flags)
    offer = conv.offer or "take the next step"

    # ---------------- opt-out: stop immediately
    if kind == "optout":
        conv.ended = True
        if not conv.customer_id:
            flags.opted_out = True
        return {"action": "end",
                "rationale": "Explicit opt-out/not-interested. Closing and suppressing all further proactive sends to this "
                             + ("customer." if conv.customer_id else "merchant.")}

    # ---------------- auto-reply: 1st = one owner-flag nudge, 2nd = back off 24h, 3rd = exit
    if kind == "auto":
        n = _norm(msg)
        if n not in flags.auto_texts:
            flags.auto_texts.append(n)
        flags.auto_count += 1
        conv.auto_count += 1
        count = max(conv.auto_count, flags.auto_count)
        if count == 1:
            text = (f"Lagta hai yeh auto-reply hai 🙂 Owner/manager jab dekhein, bas YES reply kar dein — main {offer} kar dungi."
                    if hi else f"Looks like an auto-reply 🙂 Whenever the owner sees this, just reply YES and I'll {offer}.")
            return _send(conv, text, "binary_yes_no",
                         "Detected WhatsApp Business auto-reply (canned phrasing). One short owner-flag prompt, no new pitch.")
        if count == 2:
            _backoff(conv, body.get("received_at"), 86400)
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same auto-reply again — owner not at the phone. Backing off 24h instead of burning turns."}
        conv.ended = True
        return {"action": "end",
                "rationale": f"Auto-reply received {count}x with no human response; closing to avoid spamming the merchant."}

    flags.auto_count = 0  # a human replied; reset the auto-reply streak

    # ---------------- hostile (no explicit stop): one apology + opt-out path, then exit
    if kind == "hostile":
        conv.hostile_count += 1
        if conv.hostile_count >= 2:
            conv.ended = True
            flags.opted_out = True
            return {"action": "end", "rationale": "Repeated frustration; exiting gracefully and suppressing further sends."}
        text = (f"Maaf kijiye, disturb karne ka irada nahi tha. Bas itna: main aapke liye {offer} kar sakti hoon — free, 2 minute. "
                "Nahi chahiye to STOP likh dijiye, phir message nahi aayega."
                if hi else f"Sorry for the bother — I'll keep it short. I can {offer} for you at no cost, 2 minutes of your time. "
                "If you'd rather not hear from me, reply STOP and I won't message again.")
        return _send(conv, text, "binary_yes_no",
                     "Merchant frustrated but did not opt out: brief apology, restate value once, explicit opt-out path.")

    # ---------------- change request on a draft: apply + move to confirm
    if kind == "change":
        conv.stage = "drafted"
        sents = [x for x in re.split(r"(?<=[.!?])\s+", msg.strip()) if CHANGE_REQ.search(x)] or [msg.strip()]
        req = re.sub(r"^(can|could) (we|you)\s+", "", sents[0].rstrip("?.! "), flags=re.I)
        text = (f"Done — update note kar liya: \"{req}\". Baaki plan same rahega. Reply CONFIRM to publish." if hi
                else f"Done — updated: {req}. Everything else stays as drafted. Reply CONFIRM to publish.")
        return _send(conv, text, "binary_confirm_cancel", "Merchant asked for a tweak — applied it without re-qualifying and moved to a single confirm.")

    # ---------------- commitment: switch straight to action (never re-qualify)
    if kind == "commit":
        return _commit(conv, msg, hi, category, merchant, trigger, customer)

    # ---------------- deferral
    if kind == "defer":
        secs = 86400 if re.search(r"\b(kal|tomorrow|next week)\b", msg, re.I) else 7200
        _backoff(conv, body.get("received_at"), secs)
        return {"action": "wait", "wait_seconds": secs,
                "rationale": f"Merchant asked for time; backing off {secs // 3600}h before following up."}

    # ---------------- off-topic ask: decline politely, redirect to the thread
    if kind == "offtopic":
        conv.offtopic_count += 1
        mm = OFFTOPIC.search(msg)
        what = mm.group(0) if mm else "that"
        what = what.upper() if what.lower() in ("gst", "itr") else what[0].upper() + what[1:]
        if conv.offtopic_count == 1:
            text = (f"{what} ke liye aapke CA/expert hi sahi rahenge — woh mere scope se bahar hai. "
                    f"Wapas apni baat pe: {offer} kar doon? Reply YES."
                    if hi else f"{what} is best handled by your CA/advisor — it's outside what I can do. "
                    f"Coming back to {_topic(conv)}: shall I {offer}? Reply YES.")
        else:
            text = (f"Us mein main madad nahi kar paungi, sorry. Jab ready hon, YES bhejiye — {offer} turant kar dungi."
                    if hi else f"I can't help with that one, sorry. Whenever you're ready, send YES and I'll {offer} right away.")
        return _send(conv, text, "binary_yes_no",
                     "Out-of-scope request declined politely without lecturing; redirected to the original trigger's offer.")

    # ---------------- explicit no
    if kind == "negative":
        conv.ended = True
        return {"action": "end", "rationale": "Merchant declined; closing gracefully without a further pitch."}

    # ---------------- question / other
    if kind == "question":
        return _answer(conv, msg, hi, category, merchant, trigger, customer)

    if kind == "empty":
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Empty inbound; waiting for a real reply."}

    # other: acknowledgement / small talk
    conv.nudges += 1
    if conv.stage in ("done",) or conv.nudges >= 3:
        conv.ended = True
        return {"action": "end", "rationale": "Task delivered / no new intent after repeated nudges; closing the loop politely."}
    if re.search(r"\b(thanks?|thank you|shukriya|dhanyavaad|great|nice|good|cool|👍)\b", msg, re.I) and conv.stage == "drafted":
        text = ("Pleasure! Draft ready hai — CONFIRM likhiye aur main live kar dungi." if hi
                else "Pleasure! The draft is ready — reply CONFIRM and I'll make it live.")
        return _send(conv, text, "binary_confirm_cancel", "Acknowledgement after draft; one nudge to confirm.")
    return _answer(conv, msg, hi, category, merchant, trigger, customer)


def _commit(conv, msg, hi, category, merchant, trigger, customer) -> dict:
    # customer-facing booking flows
    if conv.customer_id:
        slots = (trigger.get("payload") or {}).get("available_slots") or (trigger.get("payload") or {}).get("next_session_options") or []
        pick = SLOT_PICK.match(msg or "")
        slot = None
        if slots:
            idx = int(pick.group(1)) - 1 if pick else 0
            slot = slots[min(idx, len(slots) - 1)].get("label")
        if conv.stage == "done":
            conv.ended = True
            return {"action": "end", "rationale": "Booking already confirmed; nothing further to send."}
        conv.stage = "done"
        mname = (merchant.get("identity") or {}).get("name", "us")
        if slot:
            text = (f"Done ✅ {slot} aapke liye book ho gaya. Reschedule karna ho to bas yahin message kar dijiye."
                    if hi else f"Done ✅ You're booked for {slot}. If you need to reschedule, just reply here.")
        else:
            text = ("Done ✅ Confirm ho gaya — {m} ki team jaldi details share karegi.".format(m=mname) if hi
                    else f"Done ✅ Confirmed — the {mname} team will share the details shortly.")
        return _send(conv, text, "none", "Customer confirmed; closing the booking loop with the exact slot chosen.")

    price_line = ""
    if PRICE_Q.search(msg or ""):
        price_line = _price_facts(category, merchant, trigger)
    if conv.stage == "pitched" and conv.deliverable and any(conv.deliverable in b for b in conv.bot_bodies):
        # merchant already saw the draft while asking questions -> execute now, no extra confirm loop
        conv.stage = "done"
        text = ("Done ✅ Wahi draft final kar diya jo upar dikhaya tha — ab yeh live/sent hai. 7 din mein results share karungi."
                if hi else "Done ✅ Going ahead with the draft above — it's live/sent now. I'll share the results in 7 days.")
        return _send(conv, text, "none", "Merchant committed after already reviewing the draft — executed immediately instead of asking again.")
    if conv.stage == "pitched":
        conv.stage = "drafted"
        deliverable = conv.deliverable or "Your draft is ready."
        text = (f"Badhiya — kaam shuru. {deliverable}\n\n" if hi else f"Great — on it. {deliverable}\n\n")
        if price_line:
            text += price_line + "\n\n"
        text += ("Reply CONFIRM to make it live, ya bataiye kya badalna hai." if hi
                 else "Reply CONFIRM to make it live, or tell me what to change.")
        return _send(conv, text, "binary_confirm_cancel",
                     "Merchant committed — switched from pitch to execution immediately: delivered the draft, one confirm step.")
    if conv.stage == "drafted":
        conv.stage = "done"
        if conv.kind in PUBLISH_KINDS:
            text = ("Done ✅ Live ho gaya. 7 din baad views/calls ka update bhejungi." if hi
                    else "Done ✅ It's live. I'll send you the views/calls impact in 7 days.")
        else:
            text = ("Done ✅ Bhej diya — WhatsApp check kar lijiye. Kuch aur chahiye ho to yahin likh dijiye." if hi
                    else "Done ✅ Sent — check your WhatsApp. If you need anything else on this, just reply here.")
        return _send(conv, text, "none", "Confirmation received; executed and set expectation for a results check-in.")
    conv.ended = True
    return {"action": "end", "rationale": "Work delivered and confirmed; ending cleanly rather than adding noise."}


def offer_done(offer: str, hi: bool) -> str:
    o = (offer or "the update").strip()
    return (f"\"{o}\" ho gaya." if hi else f"\"{o}\" is done.")


def _price_facts(category, merchant, trigger) -> str:
    p = trigger.get("payload") or {}
    bits = []
    if p.get("renewal_amount"):
        from .util import inr
        bits.append(f"Renewal: {inr(p['renewal_amount'])} for the {p.get('plan', '')} plan".replace("  ", " "))
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active" and "₹" in (o.get("title") or ""):
            bits.append(o["title"])
    if not bits:
        for o in (category.get("offer_catalog") or [])[:2]:
            if "₹" in (o.get("title") or ""):
                bits.append(o["title"])
    return ("Pricing: " + "; ".join(bits[:3]) + ".") if bits else ""


def _answer(conv, msg, hi, category, merchant, trigger, customer) -> dict:
    conv.question_count += 1
    facts = {"merchant": {k: merchant.get(k) for k in ("identity", "performance", "offers", "subscription",
                                                      "customer_aggregate", "signals", "review_themes")},
             "trigger": trigger, "category_offers": [o.get("title") for o in category.get("offer_catalog", []) or []],
             "peer_stats": category.get("peer_stats"),
             "digest": [{k: d.get(k) for k in ("id", "title", "source", "summary")} for d in category.get("digest", []) or []]}
    if customer:
        facts["customer"] = customer
    history = []
    for i, b in enumerate(conv.bot_bodies):
        history.append({"from": "vera", "body": b})
    history.append({"from": "merchant", "body": msg})
    taboos = ((category.get("voice") or {}).get("vocab_taboo") or [])
    out = llm.answer(msg, history, facts, conv.offer, hi, taboos)
    if out:
        return _send(conv, out, "open_ended", "Answered the merchant's question from pushed context only (LLM, validated for grounding); nudged to next step.")

    # deterministic answer
    parts = []
    wants_draft = bool(re.search(r"\b(message|draft|say|post|look like|template|wording|text)\b", msg, re.I))
    snippet = None if wants_draft else _retrieve(msg, trigger, category, merchant)
    if snippet and not any(snippet in b for b in conv.bot_bodies):
        parts.append(snippet)
    if wants_draft and conv.deliverable:
        parts.append(("Yeh raha draft: " if hi else "Here's the draft: ") + conv.deliverable)
    if PRICE_Q.search(msg):
        pl = _price_facts(category, merchant, trigger).replace("Pricing: Renewal: ", "Renewal is ")
        parts.append(pl or ("Exact pricing main confirm karke bataungi — guess nahi karungi." if hi
                            else "I'll confirm the exact figure rather than guess."))
    p = trigger.get("payload") or {}
    if re.search(r"\b(when|kab|date|deadline|time)\b", msg, re.I):
        for k in ("deadline_iso", "date", "match_time_iso", "expires_at", "stock_runs_out_iso"):
            if p.get(k) or trigger.get(k):
                parts.append(("Date: " if not hi else "Tareekh: ") + nice_date(p.get(k) or trigger.get(k), True) + ".")
                break
    if not wants_draft and not PRICE_Q.search(msg) and (not parts or re.search(r"\b(how|kaise|what|kya|details|explain|look like|batao|bataiye|more|say)\b", msg, re.I)):
        if conv.deliverable and conv.question_count == 1:
            parts.append(("Short mein: " if hi else "Here's what you'd get: ") + conv.deliverable)
        elif conv.question_count > 1:
            parts.append("Aapke sawal ka exact data mere paas abhi nahi hai — main confirm karke bataungi, guess nahi karungi." if hi
                         else "I don't have that exact detail in front of me — I'll confirm rather than guess.")
    if conv.question_count >= 3:
        parts.append("Jab ready hon, YES bhej dijiye." if hi else "Whenever you're ready, just send YES.")
    else:
        parts.append(f"{conv.offer or 'Setup'} kar doon? Reply YES." if hi
                     else f"Shall I go ahead and {conv.offer or 'set it up'}? Reply YES.")
    text = " ".join(x for x in parts if x)
    return _send(conv, text, "binary_yes_no",
                 "Answered from available context (no invented facts) and kept a single YES CTA toward the trigger's offer.")


STOP = set("a an the is are was were be to of in on for with and or but this that these those it its my your our we you i me "
           "can could would should will do does did what how why when which who too also just only about any some more "
           "please pls tell me kya hai hain ho ka ki ke se ko".split())


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text or "") if len(x.strip()) > 12]


def _retrieve(question: str, trigger: dict, category: dict, merchant: dict) -> str | None:
    """Tiny keyword retrieval over the trigger's own evidence so answers stay grounded."""
    q = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in STOP and len(w) > 2}
    q |= {w.rstrip("s") for w in q}
    if not q:
        return None
    p = trigger.get("payload") or {}
    ids = {p.get("top_item_id"), p.get("digest_item_id"), p.get("alert_id")}
    pool = []
    for d in category.get("digest", []) or []:
        weight = 2 if d.get("id") in ids else 1
        for sent in _sentences(d.get("summary", "")) + _sentences(d.get("actionable", "") + "."):
            pool.append((weight, sent, d.get("source", "")))
    for r in merchant.get("review_themes", []) or []:
        if r.get("common_quote"):
            pool.append((1, f"Reviews on {humanize(r['theme'])}: \"{r['common_quote']}\" ({r.get('occurrences_30d')} mentions in 30 days).", ""))
    best = []
    for w, sent, src in pool:
        toks = {t.rstrip("s") for t in re.findall(r"[a-z0-9]+", sent.lower())}
        score = len(q & toks) * w
        if score >= 2 or (score >= 1 and w == 2):
            best.append((score, sent, src))
    if not best:
        return None
    best.sort(key=lambda x: -x[0])
    top = best[:2]
    src = top[0][2]
    return ("From " + src + ": " if src else "") + " ".join(t[1] for t in top)


CLOSING = "Thanks, and have a nice day! 🙏"


def handle_reply(body: dict) -> dict:
    """Every conversation closes politely: instead of a silent `end`, send one closing line
    (cta none), mark the conversation closed, and answer any later turn with `end`."""
    already_closed = False
    cid = body.get("conversation_id")
    with S().lock:
        c0 = S().convs.get(cid)
        already_closed = bool(c0 and c0.ended)
    out = _handle_reply_inner(body)
    if out.get("action") != "end" or already_closed:
        return out
    conv = S().convs.get(cid)
    msg = body.get("message") or ""
    merchant = S().get("merchant", conv.merchant_id) if conv else None
    hi = _hi(conv, msg, merchant or {}) if conv else False
    optout = bool(OPTOUT.search(msg) or HOSTILE.search(msg))
    if optout:
        text = ("Samajh gayi — ab aapko message nahi karungi. " if hi else "Understood — I won't message you again. ") + CLOSING
    elif conv and conv.auto_count:
        text = ("Koi baat nahi — jab owner free hon, bas 'Hi' bhej dijiye. " if hi
                else "No worries — whenever the owner is free, just send 'Hi'. ") + CLOSING
    else:
        text = CLOSING
    if conv:
        conv.ended = True
        conv.bot_bodies.append(text)
    return {"action": "send", "body": text, "cta": "none",
            "rationale": out.get("rationale", "") + " Closing with a single polite goodbye (no CTA); no further messages on this conversation."}
