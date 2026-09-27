"""Deterministic, context-grounded message composer.

Author: Pranav Yadav (B.Tech Mathematics & Computing, DTU)

compose(category, merchant, trigger, customer, now) -> dict

Every fact in a message comes from one of the four pushed contexts. Nothing is
invented: when a field is missing the handler falls back to a softer sentence
instead of making up a number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .util import (
    customer_lang, days_between, first_sentence, humanize, inr, merchant_prefers_hinglish,
    nice_date, nice_time, num, parse_dt, pct, rate, strip_trailing_period,
)


# =============================================================== context view
@dataclass
class Ctx:
    category: dict
    merchant: dict
    trigger: dict
    customer: dict | None
    now: datetime

    # ---- merchant basics
    @property
    def ident(self) -> dict:
        return self.merchant.get("identity", {}) or {}

    @property
    def slug(self) -> str:
        return self.merchant.get("category_slug") or self.category.get("slug") or ""

    @property
    def biz(self) -> str:
        return self.ident.get("name") or "your business"

    @property
    def owner(self) -> str:
        return (self.ident.get("owner_first_name") or "").strip()

    @property
    def biz_pos(self) -> str:
        b = self.biz
        return b + ("'" if b.endswith("s") else "'s")

    @property
    def locality(self) -> str:
        return self.ident.get("locality") or self.ident.get("city") or ""

    @property
    def perf(self) -> dict:
        return self.merchant.get("performance", {}) or {}

    @property
    def peer(self) -> dict:
        return self.category.get("peer_stats", {}) or {}

    @property
    def agg(self) -> dict:
        return self.merchant.get("customer_aggregate", {}) or {}

    @property
    def payload(self) -> dict:
        p = self.trigger.get("payload", {}) or {}
        return {} if p.get("placeholder") else p

    @property
    def kind(self) -> str:
        return self.trigger.get("kind", "generic")

    @property
    def hinglish(self) -> bool:
        return merchant_prefers_hinglish(self.merchant)

    def salutation(self) -> str:
        o = self.owner
        if self.slug == "dentists":
            if not o:
                return "Doc"
            return o if o.lower().startswith("dr") else f"Dr. {o}"
        if o:
            return o
        return f"{self.biz} team"

    def active_offers(self) -> list[str]:
        return [o.get("title") for o in self.merchant.get("offers", []) or []
                if o.get("status") == "active" and o.get("title")]

    def expired_offers(self) -> list[str]:
        return [o.get("title") for o in self.merchant.get("offers", []) or []
                if o.get("status") in ("expired", "paused") and o.get("title")]

    def catalog(self) -> list[dict]:
        return self.category.get("offer_catalog", []) or []

    def catalog_offer(self, *keywords, audience=None, prefer_service_price=True) -> str | None:
        cat = self.catalog()
        cands = []
        for o in cat:
            t = (o.get("title") or "")
            if audience and o.get("audience") != audience:
                continue
            if keywords and not any(k.lower() in t.lower() for k in keywords):
                continue
            cands.append(o)
        if not cands and keywords:
            return None
        if not cands:
            cands = cat
        if prefer_service_price:
            sp = [o for o in cands if o.get("type") == "service_at_price"]
            if sp:
                return sp[0].get("title")
        return cands[0].get("title") if cands else None

    def digest_item(self, item_id=None, kinds=()) -> dict | None:
        dg = self.category.get("digest", []) or []
        if item_id:
            for d in dg:
                if d.get("id") == item_id:
                    return d
        for k in kinds:
            for d in dg:
                if d.get("kind") == k:
                    return d
        return None

    def seasonal_note(self, month: int | None = None) -> str | None:
        month = month or self.now.month
        names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        for b in self.category.get("seasonal_beats", []) or []:
            rng = str(b.get("month_range", "")).lower()
            parts = re.findall(r"[a-z]{3}", rng)
            idx = [names.index(p) + 1 for p in parts if p in names]
            if not idx:
                continue
            if len(idx) == 1:
                ok = month == idx[0]
            else:
                a, z = idx[0], idx[-1]
                ok = a <= month <= z if a <= z else (month >= a or month <= z)
            if ok:
                return b.get("note")
        return None

    def review_theme(self, sentiment=None) -> dict | None:
        rts = self.merchant.get("review_themes", []) or []
        rts = [r for r in rts if not sentiment or r.get("sentiment") == sentiment]
        rts.sort(key=lambda r: -(r.get("occurrences_30d") or 0))
        return rts[0] if rts else None

    # ---- customer basics
    @property
    def cust_ident(self) -> dict:
        return (self.customer or {}).get("identity", {}) or {}

    def cust_greet_name(self) -> str:
        n = self.cust_ident.get("name") or ""
        m = re.search(r"\(parent:\s*([^)]+)\)", n)
        if m:
            return m.group(1).strip()
        if n.startswith("(") or not n:
            return ""
        return n.strip()

    def cust_child_name(self) -> str | None:
        n = self.cust_ident.get("name") or ""
        if "(parent:" in n:
            return n.split("(")[0].strip()
        return None

    @property
    def cust_lang(self) -> str:
        return customer_lang(self.customer or {})

    def merchant_short(self) -> str:
        if self.slug == "dentists" and self.owner:
            o = self.owner if self.owner.lower().startswith("dr") else f"Dr. {self.owner}"
            return f"{o}'s clinic"
        return self.biz


# =============================================================== output shape
@dataclass
class Draft:
    body: str
    cta: str = "binary_yes_no"
    rationale: str = ""
    offer: str = ""          # what a YES commits us to (used by the reply engine)
    deliverable: str = ""    # the concrete thing we send when merchant says yes
    template_params: list = field(default_factory=list)
    skip: str | None = None  # reason to not send at all


def _cap(x: str) -> str:
    return x[:1].upper() + x[1:] if x else x


def _cta(ctx: Ctx, en: str, hi: str | None = None) -> str:
    return _cap(hi if (hi and ctx.hinglish) else en)


def _perf_anchor(ctx: Ctx) -> str | None:
    """The single most useful performance fact about this merchant vs peers."""
    p, peer = ctx.perf, ctx.peer
    ctr, pctr = p.get("ctr"), peer.get("avg_ctr")
    calls, pcalls = p.get("calls"), peer.get("avg_calls_30d")
    if ctr is not None and pctr and ctr < pctr * 0.9:
        return f"your profile converts {rate(ctr)} of views into actions vs {rate(pctr)} for similar {ctx.slug} nearby"
    if calls is not None and pcalls and calls < pcalls * 0.8:
        return f"you got {num(calls)} calls in 30 days vs a peer average of {num(pcalls)}"
    if calls is not None and pcalls and calls > pcalls * 1.2:
        return f"you're at {num(calls)} calls in 30 days — above the peer average of {num(pcalls)}"
    if p.get("views") is not None:
        return f"{num(p.get('views'))} profile views and {num(p.get('calls', 0))} calls in the last 30 days"
    return None


# =============================================================== merchant-facing handlers
def h_research_digest(ctx: Ctx) -> Draft:
    p = ctx.payload
    item = ctx.digest_item(p.get("top_item_id") or p.get("digest_item_id"), kinds=("research", "trend", "tech"))
    if not item and isinstance(p.get("top_item"), dict):
        item = p["top_item"]
    if not item:
        return h_generic(ctx)
    s = ctx.salutation()
    src = item.get("source", "")
    src_short = src.split(",")[0] if src else "this week's digest"
    lines = [f"{s}, fresh from {src_short} — worth 2 minutes: {strip_trailing_period(item.get('title', ''))}."]
    detail = first_sentence(item.get("summary", ""))
    if item.get("trial_n") and str(item["trial_n"]) not in detail:
        if re.search(r"\btrial\b", detail):
            detail = re.sub(r"\btrial\b", f"trial (n={num(item['trial_n'])})", detail, count=1)
        else:
            detail = f"{num(item['trial_n'])}-person trial. " + detail
    if detail:
        lines.append(detail)
    seg = str(item.get("patient_segment", ""))
    hr = ctx.agg.get("high_risk_adult_count")
    if "high_risk" in seg and hr:
        lines.append(f"Relevant to your {num(hr)} high-risk adult patients.")
    elif item.get("actionable"):
        lines.append(strip_trailing_period(item["actionable"]) + ".")
    who = "patient" if ctx.slug == "dentists" else ("member" if ctx.slug == "gyms" else "customer")
    lines.append(_cta(ctx, f"Want me to pull the key points + draft a {who} WhatsApp you can share? Reply YES.",
                      f"Key points + ek {who}-WhatsApp draft bhej doon? Reply YES."))
    if src:
        lines[-1] += f" — {src}"
    return Draft(
        body=" ".join(lines),
        rationale=f"External research digest ({src}) matched to merchant's patient mix; curiosity + reciprocity, single YES CTA.",
        offer=f"pull the key points + draft the {who} WhatsApp",
        deliverable=_patient_ed_draft(ctx, item),
        template_params=[s, strip_trailing_period(item.get("title", "")), src],
    )


def _patient_ed_draft(ctx: Ctx, item: dict | None) -> str:
    lib = ctx.category.get("patient_content_library", []) or []
    if item and "fluoride" in (item.get("title", "") + item.get("summary", "")).lower():
        return ("\"Cavities keep coming back? New Indian research (JIDA) found a 3-month fluoride check-up "
                "works much better than 6-monthly for people with recent decay. Reply to book a quick check.\"")
    if lib:
        c = lib[0]
        return f"\"{c.get('title')}\" — {first_sentence(c.get('body', ''))}"
    if item:
        return f"\"{strip_trailing_period(item.get('title', ''))} — ask us what this means for you.\""
    return ""


def h_regulation(ctx: Ctx) -> Draft:
    p = ctx.payload
    item = ctx.digest_item(p.get("top_item_id") or p.get("digest_item_id"), kinds=("compliance",))
    if not item:
        return h_generic(ctx)
    s = ctx.salutation()
    deadline = p.get("deadline_iso")
    days = days_between(ctx.now, parse_dt(deadline)) if deadline else None
    head = f"{s}, compliance heads-up: {strip_trailing_period(item.get('title', ''))}."
    parts = [head, first_sentence(item.get("summary", ""))]
    rest = item.get("summary", "")[len(first_sentence(item.get("summary", ""))):].strip()
    if rest:
        parts.append(rest)
    if days is not None and days > 0:
        parts.append(f"That's {days} days from today.")
    parts.append(_cta(ctx, "Want me to draft a 1-page checklist for your team to audit this week? Reply YES.",
                      "Staff ke liye 1-page audit checklist draft kar doon? Reply YES."))
    return Draft(
        body=" ".join(x for x in parts if x) + (f" — {item.get('source')}" if item.get("source") else ""),
        rationale="Regulatory change with a hard deadline — loss aversion + effort externalization (we draft the checklist).",
        offer="draft the audit checklist for your team",
        deliverable=f"Checklist: 1) {item.get('actionable', 'Audit current setup')}. 2) Record the check in your SOP file. 3) Flag any equipment that fails before {nice_date(deadline) if deadline else 'the deadline'}.",
        template_params=[s, item.get("title", ""), nice_date(deadline) if deadline else ""],
    )


def h_cde(ctx: Ctx) -> Draft:
    p = ctx.payload
    item = ctx.digest_item(p.get("digest_item_id") or p.get("top_item_id"), kinds=("cde",))
    if not item:
        return h_generic(ctx)
    s = ctx.salutation()
    when = item.get("date")
    credits = p.get("credits") or item.get("credits")
    fee = item.get("actionable") or humanize(p.get("fee", ""))
    parts = [f"{s}, {strip_trailing_period(item.get('title', ''))}"]
    if when:
        parts[0] += f" — {nice_date(when, True)}, {nice_time(when)}"
    parts[0] += "."
    if credits:
        parts.append(f"{credits} CDE credits; {strip_trailing_period(fee)}.")
    if item.get("summary"):
        parts.append(item["summary"])
    parts.append(_cta(ctx, "Want me to block the slot and send you the registration details? Reply YES.",
                      "Slot block karke registration details bhej doon? Reply YES."))
    return Draft(
        body=" ".join(parts),
        cta="binary_yes_no",
        rationale="Professional-development item from the category calendar with date + credits; low-effort YES.",
        offer="block the slot and send the registration details",
        deliverable=f"Blocked: {item.get('title')} ({nice_date(when, True) if when else ''}). Source: {item.get('source', '')}.",
        template_params=[s, item.get("title", ""), nice_date(when, True) if when else ""],
    )


def h_perf_dip(ctx: Ctx, seasonal=False) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    metric = p.get("metric")
    delta = p.get("delta_pct")
    d7 = ctx.perf.get("delta_7d", {}) or {}
    if metric is None or delta is None:
        # derive from the merchant snapshot — pick the worst 7d metric
        worst = sorted(((k, v) for k, v in d7.items() if isinstance(v, (int, float))), key=lambda kv: kv[1])
        if worst and worst[0][1] < 0:
            metric, delta = worst[0][0].replace("_pct", ""), worst[0][1]
    if metric is None or delta is None:
        return h_peer_gap(ctx)
    metric_h = humanize(metric)
    window = p.get("window", "7d").replace("7d", "this week").replace("30d", "this month")
    base = p.get("vs_baseline")
    head = f"{s}, {metric_h} at {ctx.biz} are down {pct(delta)} {window}"
    head += f" (usual baseline ~{num(base)})." if base else "."
    parts = [head]
    note = ctx.seasonal_note()
    if seasonal or p.get("is_expected_seasonal"):
        if note:
            parts.append(f"This one's seasonal — {note}.")
        else:
            parts.append("This looks seasonal rather than something you did.")
        churn = ctx.agg.get("monthly_churn_pct")
        members = ctx.agg.get("total_active_members")
        pchurn = ctx.peer.get("monthly_churn_pct")
        if members and churn is not None:
            line = f"The lever now is keeping your {num(members)} active members: churn is {pct(churn)}/month"
            line += f" vs {pct(pchurn)} for peers." if pchurn else "."
            parts.append(line)
        offer = ctx.catalog_offer("refer", "friend", prefer_service_price=False) or (ctx.active_offers() or [None])[0]
        if offer:
            parts.append(f"I'd run '{offer}' for existing members instead of chasing new sign-ups.")
        parts.append(_cta(ctx, "Want me to draft the member WhatsApp? Reply YES.",
                          "Member WhatsApp draft kar doon? Reply YES."))
        return Draft(body=" ".join(parts),
                     rationale="Expected seasonal dip — reassure, redirect effort to retention using the category's seasonal beat + a catalog offer.",
                     offer="draft the member WhatsApp",
                     deliverable=f"\"Bring a friend this month — {offer}. Just reply with their name.\"" if offer else "",
                     template_params=[s, f"{metric_h} {pct(delta, True)}", offer or ""])
    # non-seasonal dip: diagnose from signals
    sig = " ".join(ctx.merchant.get("signals", []) or [])
    causes = []
    if not ctx.active_offers():
        causes.append("no active offer on your profile")
    if "unverified" in sig or ctx.ident.get("verified") is False:
        causes.append("the Google profile is still unverified")
    if "stale_posts" in sig or "no_recent_post" in sig:
        causes.append("no recent Google post")
    anchor = _perf_anchor(ctx)
    if anchor:
        parts.append(anchor[0].upper() + anchor[1:] + ".")
    if causes:
        parts.append("Likely contributors: " + ", ".join(causes) + ".")
    fix = ctx.catalog_offer(audience="new_user") if not ctx.active_offers() else None
    if fix:
        parts.append(f"Fastest fix: put '{fix}' live — I've drafted the listing.")
        parts.append(_cta(ctx, "Reply YES and it goes live today.", "Reply YES — aaj hi live kar deti hoon."))
        offer = f"put '{fix}' live"
        deliverable = f"Offer '{fix}' + a Google post announcing it, ready to publish."
    else:
        parts.append(_cta(ctx, "Want a 3-step recovery plan for this week? Reply YES.",
                          "Is hafte ka 3-step recovery plan bhej doon? Reply YES."))
        offer = "send the 3-step recovery plan"
        deliverable = _recovery_plan(ctx)
    return Draft(body=" ".join(parts),
                 rationale="Internal perf dip — loss aversion with the exact drop, a diagnosis grounded in merchant signals, and one concrete fix.",
                 offer=offer, deliverable=deliverable,
                 template_params=[s, f"{metric_h} {pct(delta, True)}", fix or "recovery plan"])


def _recovery_plan(ctx: Ctx) -> str:
    steps = []
    offers = ctx.active_offers()
    steps.append(f"Pin '{offers[0]}' at the top of your Google profile" if offers
                 else f"Add '{ctx.catalog_offer(audience='new_user') or 'a service+price offer'}' to your profile")
    steps.append("Publish 2 Google posts this week (I'll draft both)")
    steps.append("Ask your last 20 happy customers for a review via WhatsApp (template ready)")
    return "Plan: " + "; ".join(f"{i+1}) {x}" for i, x in enumerate(steps)) + "."


def h_perf_spike(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    metric, delta = p.get("metric"), p.get("delta_pct")
    if metric is None or delta is None:
        d7 = ctx.perf.get("delta_7d", {}) or {}
        best = sorted(((k, v) for k, v in d7.items() if isinstance(v, (int, float))), key=lambda kv: -kv[1])
        if best and best[0][1] > 0:
            metric, delta = best[0][0].replace("_pct", ""), best[0][1]
    if metric is None or delta is None:
        return h_generic(ctx, label="your profile is holding up well")
    driver = p.get("likely_driver")
    head = f"{s}, good news — {humanize(metric)} are up {pct(delta)} this week"
    if p.get("vs_baseline"):
        head += f" vs your ~{num(p['vs_baseline'])} baseline"
    head += "."
    parts = [head]
    if driver:
        parts.append(f"The likely driver is your {humanize(driver)}.")
        parts.append(_cta(ctx, "Want me to draft 2 more posts in the same format to keep the momentum? Reply YES.",
                          "Isi format mein 2 aur posts draft kar doon? Reply YES."))
        offer = f"draft 2 more posts like the {humanize(driver)}"
    else:
        anchor = _perf_anchor(ctx)
        if anchor:
            parts.append(anchor[0].upper() + anchor[1:] + ".")
        parts.append(_cta(ctx, "Worth doubling down while it's working — want me to draft this week's post? Reply YES.",
                          "Momentum pe ek aur post daal dein? Reply YES."))
        offer = "draft this week's post"
    return Draft(body=" ".join(parts),
                 rationale="Positive internal signal — reinforce what worked (named driver) and externalize the effort of repeating it.",
                 offer=offer, deliverable="2 post drafts in the same format, scheduled Tue + Fri 10am.",
                 template_params=[s, f"{humanize(metric)} {pct(delta, True)}", humanize(driver or "")])


def h_renewal(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    sub = ctx.merchant.get("subscription", {}) or {}
    days = p.get("days_remaining", sub.get("days_remaining"))
    plan = p.get("plan", sub.get("plan", ""))
    amt = p.get("renewal_amount")
    status = sub.get("status")
    if status == "expired" or (isinstance(days, (int, float)) and days <= 0):
        head = f"{s}, your {plan} plan has expired"
        if sub.get("days_since_expiry"):
            head += f" ({sub['days_since_expiry']} days ago)"
    elif status == "trial" or str(plan).lower() == "trial":
        head = f"{s}, your free trial ends in {days} days"
    elif isinstance(days, (int, float)) and days > 45:
        head = f"{s}, planning-ahead note: your {plan} plan has {days} days left, so nothing is due yet"
    else:
        head = f"{s}, your {plan} plan ends in {days} days" if days is not None else f"{s}, your {plan} plan is up for renewal"
    head += f" ({inr(amt)} to renew)." if amt else "."
    parts = [head]
    perf = ctx.perf
    if perf.get("views") is not None:
        parts.append(f"Last 30 days: {num(perf.get('views'))} profile views, {num(perf.get('calls', 0))} calls, {num(perf.get('directions', 0))} direction requests.")
    d7 = perf.get("delta_7d", {}) or {}
    if isinstance(d7.get("calls_pct"), (int, float)) and d7["calls_pct"] < -0.15:
        parts.append(f"Calls are down {pct(d7['calls_pct'])} this week, so a lapse now would compound that.")
    parts.append(_cta(ctx, "Reply YES and I'll send the renewal details plus a 7-day plan to lift calls.",
                      "Reply YES — renewal details + calls badhane ka 7-day plan bhej deti hoon."))
    return Draft(body=" ".join(parts),
                 rationale="Renewal due — specific days/amount, value recap from the merchant's own numbers, loss aversion, single YES.",
                 offer="send the renewal details + a 7-day plan", deliverable=_recovery_plan(ctx),
                 template_params=[s, str(days), inr(amt) if amt else ""])


def h_festival(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    if not p.get("festival"):
        return h_season_generic(ctx)
    fest = p.get("festival")
    date = p.get("date")
    days = days_between(ctx.now, parse_dt(date)) if date else None
    if days is None or days < 0:
        days = p.get("days_until")
    head = f"{s}, {fest} is {days} days away" if days is not None else f"{s}, {fest} is coming up"
    head += f" ({nice_date(date)})." if date else "."
    parts = [head]
    note = ctx.seasonal_note(parse_dt(date).month if parse_dt(date) else None)
    if note:
        parts.append(f"For {ctx.slug}, that window means: {note}.")
    offers = ctx.active_offers()
    if offers:
        parts.append(f"You already have {', '.join(offers[:2])} live.")
    extra = ctx.catalog_offer("bridal", "spa", "family", "diabetic", "combo", "annual") if ctx.slug != "dentists" else ctx.catalog_offer("whitening")
    if extra and extra not in offers:
        parts.append(f"I'd add '{extra}' as the {fest} hook and post it 2 weeks before.")
    parts.append(_cta(ctx, f"Want me to draft the {fest} post + WhatsApp now so it's ready? Reply YES.",
                      f"{fest} post + WhatsApp abhi draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Festival window computed from today's date; ties category seasonal beat to merchant's live + catalog offers.",
                 offer=f"draft the {fest} post + WhatsApp", deliverable=f"{fest} Google post featuring {extra or (offers[0] if offers else 'your top service')}, scheduled 14 days before.",
                 template_params=[s, fest, str(days) if days is not None else ""])


def h_curious_ask(ctx: Ctx) -> Draft:
    s = ctx.salutation()
    d7 = ctx.perf.get("delta_7d", {}) or {}
    parts = []
    if isinstance(d7.get("calls_pct"), (int, float)) and d7["calls_pct"] > 0.05:
        parts.append(f"{s}, your calls are up {pct(d7['calls_pct'])} this week — nice.")
    else:
        parts.append(f"{s}, quick one.")
    ask = {
        "salons": "Which service are people asking for most right now — bridal, hair spa, or something else?",
        "dentists": "What's the most-asked treatment at the clinic this week?",
        "restaurants": "Which dish is flying this week?",
        "gyms": "Which class or slot is filling up fastest this week?",
        "pharmacies": "What are customers asking for most this week that you run out of?",
    }.get(ctx.slug, "What's the most-asked service this week?")
    parts.append(ask)
    parts.append(_cta(ctx, "Tell me in one line and I'll turn it into a Google post + a WhatsApp reply template for you.",
                      "Ek line mein bata dijiye — main usse Google post + WhatsApp reply template bana dungi."))
    return Draft(body=" ".join(parts), cta="open_ended",
                 rationale="Scheduled curiosity check-in: asking-the-merchant lever (rare in production Vera) with a concrete payoff.",
                 offer="turn your answer into a Google post + reply template", deliverable="Post + reply template for the service named.",
                 template_params=[s, ask])


def h_winback(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    sub = ctx.merchant.get("subscription", {}) or {}
    days = p.get("days_since_expiry", sub.get("days_since_expiry"))
    parts = [f"{s}, it's been {days} days since {ctx.biz_pos} plan paused." if days else f"{s}, quick update on {ctx.biz}."]
    facts = []
    dip = p.get("perf_dip_pct")
    if dip is None:
        dip = (ctx.perf.get("delta_7d", {}) or {}).get("calls_pct")
    if isinstance(dip, (int, float)) and dip < 0:
        facts.append(f"calls are down {pct(dip)}")
    if p.get("lapsed_customers_added_since_expiry"):
        facts.append(f"{num(p['lapsed_customers_added_since_expiry'])} more customers have gone inactive")
    if facts:
        parts.append("Since then " + " and ".join(facts) + ".")
    lapsed = ctx.agg.get("lapsed_90d_plus") or ctx.agg.get("lapsed_180d_plus")
    hook = ctx.catalog_offer(audience="new_user")
    if lapsed and hook:
        parts.append(f"You have {num(lapsed)} lapsed customers — a '{hook}' comeback message to them is the quickest win.")
    parts.append(_cta(ctx, "Want me to show you the reactivation plan (no commitment)? Reply YES.",
                      "Reactivation plan dikhaun? Koi commitment nahi. Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Win-back of expired merchant: quantified loss since expiry + a no-commitment next step.",
                 offer="share the reactivation plan", deliverable=_recovery_plan(ctx),
                 template_params=[s, str(days or ""), hook or ""])


def h_dormant(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    days = p.get("days_since_last_merchant_message")
    parts = [f"{s}, been {days} days since we last spoke — one thing I spotted on {ctx.biz}." if days
             else f"{s}, one thing I spotted on {ctx.biz}."]
    anchor = _perf_anchor(ctx)
    if anchor:
        parts.append(anchor[0].upper() + anchor[1:] + ".")
    trend = sorted(ctx.category.get("trend_signals", []) or [], key=lambda t: -(t.get("delta_yoy") or 0))
    if trend:
        t = trend[0]
        parts.append(f"Meanwhile '{t['query']}' searches are up {pct(t['delta_yoy'])} year-on-year.")
    parts.append(_cta(ctx, "Want me to show how your listing can catch that demand? Reply YES.",
                      "Yeh demand aapke profile pe kaise laayein, dikhaun? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Re-engagement after silence: reciprocity (a finding about their account) + category trend as curiosity hook.",
                 offer="show how your listing can catch that demand", deliverable=_recovery_plan(ctx),
                 template_params=[s, str(days or ""), trend[0]["query"] if trend else ""])


def h_ipl(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    match, venue = p.get("match", "tonight's match"), p.get("venue")
    t = parse_dt(p.get("match_time_iso"))
    when = f"{nice_time(p.get('match_time_iso'))}" if t else "tonight"
    dow = t.astimezone().strftime("%A") if t else ""
    head = f"{s}, {match} at {venue} today, {when}." if venue else f"{s}, {match} today at {when}."
    parts = [head]
    dg = ctx.digest_item(kinds=("seasonal",))
    weekend = (p.get("is_weeknight") is False) or dow in ("Saturday", "Sunday")
    if dg and "ipl" in dg.get("title", "").lower() and weekend:
        parts.append(f"Heads-up from {dg.get('source', 'platform data')}: {strip_trailing_period(dg.get('title', ''))} — so lean on delivery, not dine-in, for this {dow or 'weekend'} game.")
    offers = ctx.active_offers()
    combo = ctx.catalog_offer("match")
    if offers and any("tue" in o.lower() for o in offers) and weekend:
        parts.append(f"Your '{offers[0]}' doesn't cover today.")
    if combo:
        parts.append(f"A '{combo}' delivery post before {when} is the quick play.")
    parts.append(_cta(ctx, "Want me to put it up now? Reply YES.", "Abhi post daal doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Same-day external event with a time deadline; uses category digest insight (weekend matches underperform) and the matching catalog combo.",
                 offer=f"put up the {combo or 'match-night'} post", deliverable=f"Google post: '{match} tonight — {combo or 'match-night special'}, delivery only. Order before {when}.'",
                 template_params=[s, match, combo or ""])


def h_review_theme(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    theme = p.get("theme")
    rt = None
    if not theme:
        rt = ctx.review_theme("neg")
        if not rt:
            return h_review_generic(ctx)
        theme = rt.get("theme")
    occ = p.get("occurrences_30d") or (rt or {}).get("occurrences_30d")
    quote = p.get("common_quote") or (rt or {}).get("common_quote")
    head = f"{s}, {occ} reviews in the last 30 days mention {humanize(theme)}" if occ else f"{s}, reviews are flagging {humanize(theme)}"
    if p.get("trend") == "rising":
        head += ", and it's rising"
    head += "."
    parts = [head]
    if quote:
        parts.append(f"Typical line: \"{quote}\".")
    pos = ctx.review_theme("pos")
    if pos and pos.get("theme") != theme:
        parts.append(f"The good news: {pos.get('occurrences_30d')} reviews praise {humanize(pos.get('theme'))}, so it's fixable, not a brand problem.")
    parts.append(_cta(ctx, "Want me to draft a calm public reply for these reviews + one fix to announce? Reply YES.",
                      "In reviews ke liye ek public reply + ek fix ka announcement draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Emerging negative review theme with count + real quote; balances with positive theme; offers ready-to-post reply.",
                 offer="draft the public review reply", deliverable=f"Reply draft: \"Thank you for flagging this — we've heard you on {humanize(theme)} and have changed how we handle it this week. Please give us another try.\"",
                 template_params=[s, humanize(theme), str(occ or "")])


def h_milestone(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    metric = humanize(p.get("metric", "reviews")).replace("review count", "Google reviews")
    now_v, target = p.get("value_now"), p.get("milestone_value")
    if now_v is None or target is None:
        return h_milestone_views(ctx)
    gap = target - now_v if isinstance(target, (int, float)) and isinstance(now_v, (int, float)) else None
    parts = [f"{s}, {ctx.biz} is at {num(now_v)} {metric} — {num(gap)} away from {num(target)}." if gap and gap > 0
             else f"{s}, {ctx.biz} just hit {num(now_v)} {metric}!"]
    peer_rev = ctx.peer.get("avg_review_count")
    if peer_rev and "review" in metric:
        parts.append(f"Peer average is {num(peer_rev)}, so you're {'ahead' if now_v >= peer_rev else 'close'}.")
    pos = ctx.review_theme("pos")
    if pos:
        parts.append(f"Your regulars already rave about {humanize(pos['theme'])} ({pos.get('occurrences_30d')} mentions this month).")
    parts.append(_cta(ctx, "Want me to draft a short review-ask WhatsApp for your repeat customers? Reply YES.",
                      "Repeat customers ke liye chhota review-ask WhatsApp draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Imminent milestone — small concrete gap creates goal-gradient motivation; social proof via peer average.",
                 offer="draft the review-ask WhatsApp",
                 deliverable=f"\"Thank you for being a regular at {ctx.biz}! If you enjoyed your last visit, a quick Google review helps us a lot 🙏\"",
                 template_params=[s, num(now_v), num(target)])


PLAN_LIBRARY = {
    "corporate_bulk_thali_package": lambda ctx: (
        "Here's a first cut of the corporate thali pack:\n"
        f"• Base: your {next((o for o in ctx.active_offers() if 'thali' in o.lower()), 'weekday lunch thali')}\n"
        "• For offices within delivery range, min 10 thalis/day, order by 11am for 1pm drop\n"
        "• Weekly menu shared every Monday; monthly billing for the office admin\n"
        f"• Pitch line for your Google post: \"Office lunch sorted — {ctx.biz} thalis delivered daily.\""
    ),
    "kids_yoga_summer_camp": lambda ctx: (
        "Here's a first draft of the kids yoga camp:\n"
        "• 4 weeks, 3 sessions/week, 45 min, ages 6-12\n"
        "• Weekend morning batch for working parents + one weekday evening batch\n"
        "• Week 1 balance & breathing → week 4 a small parents' demo day\n"
        f"• Launch hook: a free trial class, then '{next(iter(ctx.active_offers()), 'First Month @ ₹499')}'"
    ),
}


def h_planning(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    topic = p.get("intent_topic", "")
    last = p.get("merchant_last_message")
    plan_fn = PLAN_LIBRARY.get(topic)
    if plan_fn:
        plan = plan_fn(ctx)
    else:
        plan = (f"Here's a first draft for {humanize(topic)}:\n• Who it's for and one clear price point"
                f" (anchored on {next(iter(ctx.active_offers()), ctx.catalog_offer() or 'your best-selling service')})\n"
                "• Where it goes live: Google post + WhatsApp to your regulars\n• Start date: this week")
    intro = f"{s}, picking up on your message" + (f" (\"{last}\")" if last else "") + "."
    body = f"{intro} {plan}\n" + _cta(ctx, "Reply YES to publish it as-is, or tell me what to change.",
                                        "Reply YES to publish — ya bataiye kya badlun.")
    return Draft(body=body, cta="binary_yes_no",
                 rationale="Merchant already expressed intent — skip qualifying, deliver a concrete draft immediately (intent → action).",
                 offer=f"publish the {humanize(topic)} plan", deliverable="Publishing it as a Google post + a WhatsApp to your regulars, using the plan above.",
                 template_params=[s, humanize(topic), "draft"])


def h_supply_alert(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    item = ctx.digest_item(p.get("alert_id"), kinds=("alert",))
    mol = p.get("molecule") or ""
    batches = p.get("affected_batches") or []
    mfr = p.get("manufacturer")
    head = f"{s}, urgent: voluntary recall on {mol} batches {', '.join(batches)}" if batches else f"{s}, urgent: recall alert on {mol}"
    head += f" ({mfr})" if mfr else ""
    head += f" — {item.get('source')}." if item and item.get("source") else "."
    parts = [head]
    crx = ctx.agg.get("chronic_rx_count")
    if crx:
        parts.append(f"You have {num(crx)} chronic-Rx customers; anyone on {mol} needs a batch check before their next refill.")
    parts.append("Pull these batches from the shelf today.")
    parts.append(_cta(ctx, f"Want me to pull your {mol} refill customer list + draft the WhatsApp to them? Reply YES.",
                      f"{mol} refill customers ki list + unke liye WhatsApp draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Urgency-5 safety recall: exact batches/manufacturer, merchant's chronic-Rx base, one protective action.",
                 offer=f"pull your {mol} customer list + draft the WhatsApp",
                 deliverable=f"\"Namaste, {ctx.biz} here. A batch of {mol} has been voluntarily recalled. Please don't discard your medicine — share a photo of the strip and we'll check the batch and replace it free if affected.\"",
                 template_params=[s, mol, ", ".join(batches)])


def h_category_seasonal(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    trends = p.get("trends") or []
    parsed = []
    for t in trends:
        m = re.match(r"(.+?)_demand_([+-]?\d+)", str(t))
        if m:
            parsed.append(f"{humanize(m.group(1)).replace('cold cough', 'cold/cough')} {int(m.group(2)):+d}%")
        else:
            parsed.append(humanize(t))
    season = humanize(p.get("season", "this season"))
    parts = [f"{s}, {season} demand shift is here: " + ", ".join(parsed) + "." if parsed else f"{s}, the {season} demand shift has started."]
    item = ctx.digest_item(kinds=("seasonal",))
    if item and item.get("source"):
        parts[-1] = parts[-1][:-1] + f" ({item['source']})."
    if p.get("shelf_action_recommended"):
        parts.append("Worth moving the up-items to the counter and trimming reorders on the down-items.")
    lib = [c for c in ctx.category.get("patient_content_library", []) or [] if "summer" in c.get("id", "") + c.get("title", "").lower()]
    tot = ctx.agg.get("total_unique_ytd")
    if lib:
        parts.append(f"I also have a ready '{lib[0]['title']}' WhatsApp you can send" + (f" to your {num(tot)} customers." if tot else "."))
    parts.append(_cta(ctx, "Want the reorder checklist + that message? Reply YES.", "Reorder checklist + message bhej doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Category-wide seasonal demand shift with exact deltas; shelf action + ready customer content (effort externalization).",
                 offer="send the reorder checklist + customer WhatsApp",
                 deliverable=(lib[0].get("body", "") if lib else "Reorder checklist for the up-trending items."),
                 template_params=[s, season, ", ".join(parsed)])


def h_gbp_unverified(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    path = humanize(p.get("verification_path", "postcard_or_phone_call")).replace(" or ", " or a ")
    up = p.get("estimated_uplift_pct")
    parts = [f"{s}, {ctx.biz_pos} Google profile is still unverified — so Google treats every edit as provisional."]
    if up:
        parts.append(f"Verified listings typically get ~{pct(up)} more visibility.")
    anchor = _perf_anchor(ctx)
    if anchor:
        parts.append(f"Right now {anchor}.")
    parts.append(f"Verification is via a {path}; I'll walk you through it in 5 minutes.")
    parts.append(_cta(ctx, "Reply YES to start.", "Reply YES — shuru karte hain."))
    return Draft(body=" ".join(parts),
                 rationale="Unverified GBP: quantified uplift from trigger payload, merchant's current numbers, effort externalization.",
                 offer="walk you through verification", deliverable="Step 1: open Google Business Profile → 'Get verified' → choose phone call. I'll stay on this chat for each step.",
                 template_params=[s, pct(up) if up else "", path])


def h_competitor(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    name, dist, their = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer")
    if not name:
        return h_competitor_generic(ctx)
    head = f"{s}, {name} opened {dist} km from you" if dist else f"{s}, {name} opened near you"
    head += f" on {nice_date(p['opened_date'])}" if p.get("opened_date") else ""
    head += f", leading with '{their}'." if their else "."
    parts = [head]
    mine = ctx.active_offers()
    m_price = re.search(r"₹\s?([\d,]+)", mine[0]) if mine else None
    t_price = re.search(r"₹\s?([\d,]+)", their or "")
    if m_price and t_price:
        diff = int(m_price.group(1).replace(",", "")) - int(t_price.group(1).replace(",", ""))
        if diff > 0:
            parts.append(f"That's {inr(diff)} under your '{mine[0]}'.")
    pos = ctx.review_theme("pos")
    if pos:
        q = f" (\"{pos['common_quote']}\")" if pos.get("common_quote") else ""
        parts.append(f"I wouldn't price-match — your edge is {humanize(pos['theme'])}: {pos.get('occurrences_30d')} reviews this month{q}.")
    else:
        parts.append("I wouldn't price-match — compete on trust and reviews instead.")
    parts.append(_cta(ctx, "Want me to draft a Google post that leads with that? Reply YES.",
                      "Isi pe ek Google post draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale="Competitor opened nearby (from trigger payload only); loss aversion + differentiation on the merchant's own positive review theme.",
                 offer="draft that Google post", deliverable=f"Post: \"{(pos or {}).get('common_quote', 'Patients love our care')} — see why {ctx.locality} trusts {ctx.biz}.\"",
                 template_params=[s, name, their or ""])


def h_peer_gap(ctx: Ctx) -> Draft:
    """perf_dip with no explicit delta: frame the gap vs local peers (only real numbers)."""
    s = ctx.salutation()
    p, peer = ctx.perf, ctx.peer
    gaps = []
    for key, pkey, label in (("calls", "avg_calls_30d", "calls"), ("views", "avg_views_30d", "profile views"),
                             ("directions", "avg_directions_30d", "direction requests")):
        v, pv = p.get(key), peer.get(pkey)
        if isinstance(v, (int, float)) and pv and v < pv:
            gaps.append((1 - v / pv, label, v, pv))
    ctr, pctr = p.get("ctr"), peer.get("avg_ctr")
    if not gaps and not (ctr and pctr and ctr < pctr):
        return h_generic(ctx)
    parts = []
    if gaps:
        gaps.sort(reverse=True)
        g = gaps[0]
        parts.append(f"{s}, {ctx.biz} is trailing nearby {ctx.slug} on {g[1]}: {num(g[2])} in the last 30 days vs a peer average of {num(g[3])}.")
    else:
        parts.append(f"{s}, {ctx.biz_pos} profile turns {rate(ctr)} of views into calls/directions vs {rate(pctr)} for peers.")
    if not ctx.active_offers():
        fix = ctx.catalog_offer(audience="new_user")
        if fix:
            parts.append(f"You have no live offer — '{fix}' is what's converting best for {ctx.slug} right now; I've drafted it.")
            parts.append(_cta(ctx, "Reply YES and it goes live today.", "Reply YES — aaj hi live kar deti hoon."))
            return Draft(body=" ".join(parts), rationale="Dip vs peers from real 30-day numbers; single concrete fix (catalog service+price offer).",
                         offer=f"put '{fix}' live", deliverable=f"Offer '{fix}' + a Google post announcing it, ready to publish.",
                         template_params=[s, gaps[0][1] if gaps else "conversion", fix])
    parts.append(_cta(ctx, "Want the 3-step plan to close that gap this week? Reply YES.",
                      "Yeh gap is hafte band karne ka 3-step plan bhej doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Dip vs peers from real 30-day numbers; effort externalised into a ready plan.",
                 offer="send the 3-step plan", deliverable=_recovery_plan(ctx),
                 template_params=[s, gaps[0][1] if gaps else "conversion", "plan"])


def h_review_generic(ctx: Ctx) -> Draft:
    s = ctx.salutation()
    peer_rev = ctx.peer.get("avg_review_count")
    parts = [f"{s}, new reviews are coming in for {ctx.biz}, and how fast you reply to them is visible to every searcher."]
    if peer_rev:
        parts.append(f"Local {ctx.slug} average {num(peer_rev)} reviews; replying to each within 24h is the cheapest trust signal you can add.")
    parts.append(_cta(ctx, "Want me to draft reply templates (happy / unhappy / neutral) you can reuse? Reply YES.",
                      "Happy / unhappy / neutral — teeno ke reply templates draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Review-theme trigger without theme detail: no invented quotes; offers reusable reply templates, grounded in peer review average.",
                 offer="draft the reply templates",
                 deliverable="Happy: \"Thank you so much — see you again soon!\" | Unhappy: \"Sorry about this — please WhatsApp us so we can fix it.\" | Neutral: \"Thanks for the feedback — we're working on it.\"",
                 template_params=[s, "review replies", str(peer_rev or "")])


def h_milestone_views(ctx: Ctx) -> Draft:
    s = ctx.salutation()
    v = ctx.perf.get("views")
    if not isinstance(v, (int, float)) or v < 500:
        return h_generic(ctx)
    step = 1000 if v >= 1000 else 500
    mark = int(v // step * step)
    parts = [f"{s}, milestone: {ctx.biz} crossed {num(mark)} Google profile views in the last 30 days ({num(v)} exactly)."]
    pv = ctx.peer.get("avg_views_30d")
    if pv:
            parts.append(f"Peer average is {num(pv)}, so you're " + ("ahead." if v >= pv else ("close." if v >= 0.7 * pv else "early — lots of headroom.")))
    parts.append(f"Next step is turning more of those into calls — you're at {num(ctx.perf.get('calls', 0))}.")
    parts.append(_cta(ctx, "Want me to draft a 'thank you, {n} views' post with your best offer? Reply YES.".replace("{n}", num(mark)),
                      "Ek 'thank you' post best offer ke saath draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Milestone trigger without payload values: derived a true milestone from the merchant's real 30-day views; goal-gradient + peer comparison.",
                 offer="draft the milestone post", deliverable=f"Post: \"{num(mark)}+ of you found us on Google this month — thank you! {next(iter(ctx.active_offers()), '')}\"",
                 template_params=[s, num(mark), "views"])


def h_competitor_generic(ctx: Ctx) -> Draft:
    s = ctx.salutation()
    parts = [f"{s}, a new {ctx.slug.rstrip('s')} listing has come up near {ctx.locality} — searchers will now compare you side by side."]
    anchor = _perf_anchor(ctx)
    if anchor:
        parts.append(anchor[0].upper() + anchor[1:] + ".")
    offers = ctx.active_offers()
    hook = offers[0] if offers else ctx.catalog_offer(audience="new_user")
    if hook:
        parts.append(f"Best defence is a fresh post leading with '{hook}' and your reviews, not a price cut.")
    parts.append(_cta(ctx, "Want me to draft it? Reply YES.", "Draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Competitor trigger without competitor details: no names invented; defend with merchant's real numbers + offer.",
                 offer="draft that Google post", deliverable=f"Post draft leading with '{hook}' and your top review.",
                 template_params=[s, "new competitor", hook or ""])


def h_season_generic(ctx: Ctx) -> Draft:
    s = ctx.salutation()
    note, rng = None, None
    for ahead in (0, 1, 2):
        m = (ctx.now.month + ahead - 1) % 12 + 1
        note = ctx.seasonal_note(m)
        if note:
            rng = next((b.get("month_range") for b in ctx.category.get("seasonal_beats", []) if b.get("note") == note), None)
            break
    if not note:
        return h_generic(ctx)
    parts = [f"{s}, seasonal planning note for {ctx.slug}" + (f" ({rng})" if rng else "") + f": {note}."]
    offers = ctx.active_offers()
    if offers:
        parts.append(f"Your '{offers[0]}' is live; a seasonal post around it 2 weeks ahead usually catches the early bookers.")
    parts.append(_cta(ctx, "Want me to draft this season's plan + post now? Reply YES.", "Is season ka plan + post abhi draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Festival/seasonal trigger without a named festival: used the category's own seasonal beat; no invented dates.",
                 offer="draft the seasonal plan + post", deliverable="Seasonal Google post draft, scheduled 14 days ahead.",
                 template_params=[s, note, offers[0] if offers else ""])


KIND_LABEL = {
    "perf_dip": "a dip worth fixing this week", "perf_spike": "your profile is picking up",
    "review_theme_emerged": "a pattern in your reviews", "competitor_opened": "new competition nearby",
    "festival_upcoming": "the upcoming festival window", "milestone_reached": "a milestone within reach",
    "renewal_due": "your plan renewal", "dormant_with_vera": "a quick catch-up",
}


def h_generic(ctx: Ctx, label: str | None = None) -> Draft:
    s = ctx.salutation()
    p = ctx.payload
    facts = []
    for k, v in list(p.items())[:4]:
        if isinstance(v, (str, int, float)) and not str(k).endswith("_id") and k not in ("category",):
            vv = pct(v) if "pct" in k and isinstance(v, float) else (humanize(v) if isinstance(v, str) else num(v))
            facts.append(f"{humanize(k)}: {vv}")
    kind = label or KIND_LABEL.get(ctx.kind) or humanize(ctx.kind)
    parts = [f"{s}, quick one on {ctx.biz} — {kind}" + (f" ({'; '.join(facts)})." if facts else ".")]
    anchor = _perf_anchor(ctx)
    if anchor:
        parts.append(anchor[0].upper() + anchor[1:] + ".")
    offers = ctx.active_offers()
    hook = offers[0] if offers else ctx.catalog_offer(audience="new_user")
    if hook:
        parts.append(f"I can build this week's Google post around '{hook}'.")
    parts.append(_cta(ctx, "Want me to draft it? Reply YES.", "Draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts),
                 rationale=f"Trigger '{ctx.kind}' with limited payload — anchored on the merchant's real performance vs peers and a real offer; no invented facts.",
                 offer="draft this week's post", deliverable=_recovery_plan(ctx),
                 template_params=[s, kind, hook or ""])


# =============================================================== customer-facing handlers
def _consent_ok(ctx: Ctx) -> str | None:
    c = ctx.customer or {}
    prefs = c.get("preferences", {}) or {}
    consent = c.get("consent", {}) or {}
    if not consent.get("opted_in_at") and not consent.get("scope"):
        return "no recorded consent for this customer"
    if prefs.get("reminder_opt_in") is False and not consent.get("scope"):
        return "customer has not opted in"
    return None


def _cust_open(ctx: Ctx) -> str:
    name = ctx.cust_greet_name()
    lang = ctx.cust_lang
    emoji = {"dentists": " 🦷", "salons": " 💇", "gyms": " 💪", "restaurants": " 🍽️", "pharmacies": ""}.get(ctx.slug, "")
    name = re.sub(r"^(mr|mrs|ms|dr)\.?\s+", "", name, flags=re.I) if lang == "hi" else name
    if lang == "hi":
        return f"Namaste{(' ' + name + ' ji') if name else ''}! {ctx.merchant_short()} se{emoji or '.'}"
    return f"Hi{(' ' + name) if name else ''}, {ctx.merchant_short()} here{emoji or '.'}"


def _visit_word(ctx: Ctx) -> str:
    return {"gyms": "session", "restaurants": "order", "pharmacies": "visit"}.get(ctx.slug, "visit")


def _days_since_last(ctx: Ctx) -> int | None:
    rel = (ctx.customer or {}).get("relationship", {}) or {}
    d = days_between(parse_dt(rel.get("last_visit")), ctx.now) if rel.get("last_visit") else None
    return d if d is not None and d > 0 else None


def h_customer_generic(ctx: Ctx) -> Draft:
    """Customer message when the trigger payload is thin: anchored on the customer's own history."""
    c = ctx.customer or {}
    rel = c.get("relationship", {}) or {}
    lang = ctx.cust_lang
    parts = [_cust_open(ctx)]
    days = _days_since_last(ctx)
    visits = rel.get("visits_total")
    vw = _visit_word(ctx)
    if lang in ("hi", "hinglish"):
        if days:
            parts.append(f"Aapki last {vw} ko {days} din ho gaye ({nice_date(rel.get('last_visit'))})"
                         + (f" — {visits} visits ke liye shukriya!" if visits and visits > 1 else "."))
    else:
        if days:
            parts.append(f"It's been {days} days since your last {vw} on {nice_date(rel.get('last_visit'))}"
                         + (f" — thanks for the {visits} visits so far!" if visits and visits > 1 else "."))
    offers = ctx.active_offers()
    if offers:
        parts.append((f"Abhi chal raha hai: {offers[0]}." if lang != "en" else f"Running now: {offers[0]}."))
    ask = {"dentists": ("Check-up book karein?", "Shall we book your check-up?"),
           "salons": ("Aapka next appointment book karein?", "Shall we book your next appointment?"),
           "gyms": ("Is hafte wapas shuru karein?", "Want to get back to it this week?"),
           "restaurants": ("Aaj ka order/table book karein?", "Shall we save you a table or set up your order?"),
           "pharmacies": ("Aapki regular dawaiyan ready rakhein?", "Shall we keep your regular medicines ready?")}.get(
        ctx.slug, ("Book karein?", "Shall we book you in?"))
    parts.append((ask[0] + " Reply YES.") if lang != "en" else (ask[1] + " Reply YES."))
    return Draft(body=" ".join(parts), cta="binary_yes_no",
                 rationale=f"Customer-facing '{ctx.kind}' with a thin payload — anchored only on the customer's real visit history and the merchant's live offer; language pref honoured.",
                 offer="book the next visit", deliverable="booking confirmation",
                 template_params=[ctx.cust_greet_name(), ctx.biz, str(days or ""), offers[0] if offers else ""])


def _slots(ctx: Ctx) -> list[dict]:
    p = ctx.payload
    return p.get("available_slots") or p.get("next_session_options") or []


def _slot_cta(ctx: Ctx, slots: list[dict]) -> str:
    labels = [s.get("label") for s in slots if s.get("label")]
    lang = ctx.cust_lang
    if len(labels) >= 2:
        if lang in ("hi", "hinglish"):
            return (f"Aapke liye {len(labels)} slots ready hain: " + " ya ".join(labels[:3]) + ". "
                    + ", ".join(f"Reply {i+1} for {l.split(',')[0]}" for i, l in enumerate(labels[:3]))
                    + ", ya apna time bata dijiye.")
        return ("Two slots are held for you: " + " or ".join(labels[:3]) + ". "
                + ", ".join(f"Reply {i+1} for {l.split(',')[0]}" for i, l in enumerate(labels[:3]))
                + ", or tell us a time that works.")
    if len(labels) == 1:
        return (f"Next slot: {labels[0]}. Hold kar dein? Reply YES." if lang in ("hi", "hinglish")
                else f"Next slot: {labels[0]}. Shall we hold it for you? Reply YES.")
    return "Reply YES and we'll share available times." if lang == "en" else "Reply YES — hum time slots bhej denge."


def h_recall_due(ctx: Ctx) -> Draft:
    p = ctx.payload
    if not p.get("service_due") and not _slots(ctx):
        return h_customer_generic(ctx)
    c = ctx.customer or {}
    rel = c.get("relationship", {}) or {}
    svc = humanize(p.get("service_due", "check-up")).replace("6 month", "6-month")
    last = p.get("last_service_date") or rel.get("last_visit")
    lang = ctx.cust_lang
    parts = [_cust_open(ctx)]
    if lang in ("hi", "hinglish"):
        parts.append(f"Aapki last visit {nice_date(last)} ko thi — ab aapka {svc} recall due hai." if last else f"Aapka {svc} recall due hai.")
    else:
        parts.append(f"Your last visit was on {nice_date(last)}, so your {svc} recall is due." if last else f"Your {svc} recall is due.")
    offers = ctx.active_offers()
    match = next((o for o in offers if any(w in o.lower() for w in svc.lower().split())), None) or (offers[0] if offers else None)
    if match:
        parts.append(f"{match}.")
    slots = _slots(ctx)
    parts.append(_slot_cta(ctx, slots))
    return Draft(body=" ".join(parts), cta="multi_choice_slot" if len(slots) >= 2 else "binary_yes_no",
                 rationale=f"Customer recall on merchant's behalf; honours {c.get('identity', {}).get('language_pref', 'en')} language pref, real slots from trigger, merchant's live offer; consent scope includes reminders.",
                 offer="book slot", deliverable="booking confirmation",
                 template_params=[ctx.cust_greet_name(), ctx.merchant_short(), svc, " / ".join(s.get("label", "") for s in slots), match or ""])


def h_customer_winback(ctx: Ctx, hard=True) -> Draft:
    p = ctx.payload
    c = ctx.customer or {}
    rel = c.get("relationship", {}) or {}
    prefs = c.get("preferences", {}) or {}
    lang = ctx.cust_lang
    if not p:
        return h_customer_generic(ctx)
    parts = [_cust_open(ctx)]
    days = p.get("days_since_last_visit") or _days_since_last(ctx)
    focus = humanize(p.get("previous_focus") or prefs.get("training_focus") or "")
    months = p.get("previous_membership_months")
    if lang in ("hi", "hinglish"):
        parts.append(f"{days} din ho gaye aapki last visit ko — aapki kami mehsoos hui!" if days else "Kaafi time ho gaya — aapki kami mehsoos hui!")
    else:
        line = f"It's been {days} days since your last session" if days else "It's been a while since your last visit"
        if months and focus:
            line += f" — you put in {months} solid months on {focus}; no need to lose that momentum."
        else:
            line += "."
        parts.append(line)
    offers = ctx.active_offers()
    slot = humanize(prefs.get("preferred_slots", "")) if prefs.get("preferred_slots") else None
    if offers:
        parts.append(f"{offers[0]} to restart" + (f", in your usual {slot} slot." if slot else "."))
    parts.append("Reply YES and we'll book you in." if lang == "en" else "Reply YES — hum booking kar denge.")
    return Draft(body=" ".join(parts), cta="binary_yes_no",
                 rationale="Lapsed customer win-back on merchant's behalf: days since last visit, their past focus, real offer, preferred slot; no pressure/claims.",
                 offer="book comeback session", deliverable="booking confirmation",
                 template_params=[ctx.cust_greet_name(), ctx.biz, str(days or ""), offers[0] if offers else ""])


def h_trial_followup(ctx: Ctx) -> Draft:
    p = ctx.payload
    if not p.get("trial_date") and not _slots(ctx):
        return h_customer_generic(ctx)
    child = ctx.cust_child_name()
    parts = [_cust_open(ctx)]
    td = p.get("trial_date")
    on = f" on {nice_date(td)}" if td else ""
    parts.append(f"Hope {child} enjoyed the trial class{on}!" if child else f"Hope you enjoyed your trial class{on}!")
    slots = _slots(ctx)
    parts.append(_slot_cta(ctx, slots))
    return Draft(body=" ".join(parts), cta="binary_yes_no" if len(slots) < 2 else "multi_choice_slot",
                 rationale="Post-trial follow-up to the parent/customer with the exact next session from the trigger; one-tap confirm.",
                 offer="hold next session", deliverable="booking confirmation",
                 template_params=[ctx.cust_greet_name(), nice_date(td) if td else "", slots[0].get("label") if slots else ""])


def h_refill(ctx: Ctx) -> Draft:
    p = ctx.payload
    c = ctx.customer or {}
    name = re.sub(r"^(mr|mrs|ms|dr)\.?\s+", "", ctx.cust_greet_name(), flags=re.I)
    mols = p.get("molecule_list") or []
    if not mols:
        return h_customer_generic(ctx)
    runs_out = p.get("stock_runs_out_iso")
    lang = ctx.cust_lang
    offers = ctx.active_offers()
    senior = (c.get("identity", {}) or {}).get("senior_citizen")
    recall_note = None
    for d in ctx.category.get("digest", []) or []:
        if d.get("kind") == "alert" and any(m.lower() in (d.get("title", "") + d.get("summary", "")).lower() for m in mols):
            mol = next(m for m in mols if m.lower() in (d.get("title", "") + d.get("summary", "")).lower())
            recall_note = mol
    mol_txt = ", ".join(mols[:-1]) + (" aur " if lang != "en" else " and ") + mols[-1] if len(mols) > 1 else (mols[0] if mols else "dawaiyan")
    if lang in ("hi", "hinglish"):
        parts = [f"Namaste! {ctx.biz} se."]
        parts.append(f"{name}{' ji' if name and not name.endswith('ji') else ''} ki {mol_txt} {nice_date(runs_out)} tak khatam ho jayengi." if runs_out else f"{name} ji ki {mol_txt} refill due hai.")
        if p.get("delivery_address_saved"):
            parts.append("Saved address pe ghar tak delivery kar dein?")
        perks = [o for o in offers if "senior" in o.lower() and senior] + [o for o in offers if "delivery" in o.lower()]
        if perks:
            parts.append("Lagu hoga: " + " + ".join(perks) + ".")
        if recall_note:
            parts.append(f"{recall_note} ka batch hum recall list se check karke hi bhejenge.")
        parts.append("Reply HAAN to confirm.")
    else:
        parts = [_cust_open(ctx), f"The {mol_txt} refill runs out on {nice_date(runs_out)}." if runs_out else f"Your {mol_txt} refill is due."]
        if p.get("delivery_address_saved"):
            parts.append("Shall we deliver to your saved address?")
        if recall_note:
            parts.append(f"We'll check the {recall_note} batch against the current recall before dispatch.")
        parts.append("Reply YES to confirm.")
    return Draft(body=" ".join(parts), cta="binary_yes_no",
                 rationale="Chronic refill before stock-out date; Hindi per customer pref, saved-address delivery, real offers, cross-checks the live atorvastatin recall in category digest.",
                 offer="confirm refill delivery", deliverable="delivery confirmation",
                 template_params=[name, mol_txt, nice_date(runs_out) if runs_out else ""])


def h_bridal(ctx: Ctx) -> Draft:
    p = ctx.payload
    name = ctx.cust_greet_name()
    wd = p.get("wedding_date") or ((ctx.customer or {}).get("preferences", {}) or {}).get("wedding_date")
    days = days_between(ctx.now, parse_dt(wd)) if wd else None
    if days is None or days < 0:
        days = p.get("days_to_wedding")
    parts = [_cust_open(ctx).replace(" 💇", " 💐")]
    if p.get("trial_completed"):
        parts.append(f"Loved having you for your bridal trial on {nice_date(p['trial_completed'])}.")
    if wd:
        parts.append(f"{days} days to go for {nice_date(wd)}!" if days else f"Your big day is {nice_date(wd)}!")
    raw = str(p.get("next_step_window_open", ""))
    dm = re.search(r"_?(\d+)\s*day", raw)
    nxt = humanize(re.sub(r"_?\d+\s*day", "", raw))
    if dm:
        nxt = f"{dm.group(1)}-day {nxt}"
    if nxt:
        parts.append(f"This is the right window to start the {nxt} so your skin is at its best on the day.")
    pref = ((ctx.customer or {}).get("preferences", {}) or {}).get("preferred_slots")
    parts.append(f"Want us to block your {humanize(pref).title()} slots for it? Reply YES." if pref else "Want us to share the plan? Reply YES.")
    return Draft(body=" ".join(parts), cta="binary_yes_no",
                 rationale="Bridal follow-up timed to the open prep window, countdown to wedding date, customer's Saturday preference.",
                 offer="block prep program slots", deliverable="booking confirmation",
                 template_params=[name, nice_date(wd) if wd else "", nxt])


def h_appointment_tomorrow(ctx: Ctx) -> Draft:
    p = ctx.payload
    lang = ctx.cust_lang
    t = p.get("appointment_iso") or p.get("slot_iso")
    parts = [_cust_open(ctx)]
    when = f" at {nice_time(t)}" if t else ""
    if lang in ("hi", "hinglish"):
        parts.append(f"Kal{when} aapka appointment hai. Reply 1 to confirm, 2 to reschedule.")
    else:
        noun = "table booking" if ctx.slug == "restaurants" else "appointment"
        parts.append(f"Reminder: your {noun} is tomorrow{when}. Reply 1 to confirm, 2 to reschedule.")
    return Draft(body=" ".join(parts), cta="multi_choice_slot",
                 rationale="Appointment reminder the day before; confirm/reschedule options reduce no-shows.",
                 offer="confirm appointment", deliverable="confirmation",
                 template_params=[ctx.cust_greet_name(), ctx.merchant_short(), nice_time(t) if t else ""])


HEAT_PLAY = {
    "pharmacies": ("ORS, sunscreen and electrolyte demand jumps on days like this", "delivery"),
    "restaurants": ("footfall drops in the afternoon heat and delivery orders rise", "delivery"),
    "gyms": ("members skip midday sessions — early-morning and evening slots fill instead", "slots"),
    "salons": ("walk-ins dip in afternoon heat; post-sun skin and hair care gets asked about", "care"),
    "dentists": ("patients push non-urgent visits to evenings in extreme heat", "slots"),
}


def h_weather(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    temp = p.get("temp_c") or p.get("temperature_c") or p.get("max_temp_c")
    city = p.get("city") or ctx.ident.get("city", "")
    date = p.get("date")
    head = f"{s}, heat alert for {city}" + (f": {temp}°C" if temp else "") + (f" on {nice_date(date, True)}" if date else " today") + "."
    parts = [head]
    play = HEAT_PLAY.get(ctx.slug)
    if play:
        parts.append(f"For {ctx.slug}, {play[0]}.")
    offers = ctx.active_offers()
    hook = next((o for o in offers if "deliver" in o.lower()), None) or (offers[0] if offers else None)
    if hook:
        parts.append(f"A same-day post around '{hook}' catches people who won't step out.")
    parts.append(_cta(ctx, "Want me to put up a heat-day post now? Reply YES.", "Heat-day post abhi daal doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="External weather trigger with the exact temperature/date; category-specific demand shift + merchant's live offer.",
                 offer="put up the heat-day post", deliverable=f"Post: \"Too hot to step out? {hook or 'We deliver'} — WhatsApp us your order.\"",
                 template_params=[s, f"{temp}°C" if temp else "heatwave", hook or ""])


def h_news(ctx: Ctx) -> Draft:
    p = ctx.payload
    s = ctx.salutation()
    headline = p.get("headline") or p.get("title") or p.get("event")
    if not headline:
        return h_generic(ctx)
    parts = [f"{s}, local update: {strip_trailing_period(str(headline))}" + (f" ({p['source']})" if p.get("source") else "") + "."]
    if p.get("impact") or p.get("summary"):
        parts.append(strip_trailing_period(str(p.get("impact") or p.get("summary"))) + ".")
    offers = ctx.active_offers()
    if offers:
        parts.append(f"Worth a quick post so nearby customers know you're open — leading with '{offers[0]}'.")
    parts.append(_cta(ctx, "Want me to draft it? Reply YES.", "Draft kar doon? Reply YES."))
    return Draft(body=" ".join(parts), rationale="Local news event from trigger payload only; timely post with a real offer.",
                 offer="draft the update post", deliverable="Short Google post: open as usual + your live offer.",
                 template_params=[s, str(headline), offers[0] if offers else ""])


MERCHANT_HANDLERS = {
    "research_digest": h_research_digest,
    "category_research_digest_release": h_research_digest,
    "category_trend_movement": h_dormant,
    "regulation_change": h_regulation,
    "cde_opportunity": h_cde,
    "perf_dip": h_perf_dip,
    "seasonal_perf_dip": lambda c: h_perf_dip(c, seasonal=True),
    "perf_spike": h_perf_spike,
    "renewal_due": h_renewal,
    "festival_upcoming": h_festival,
    "curious_ask_due": h_curious_ask,
    "scheduled_recurring": h_curious_ask,
    "winback_eligible": h_winback,
    "dormant_with_vera": h_dormant,
    "ipl_match_today": h_ipl,
    "review_theme_emerged": h_review_theme,
    "milestone_reached": h_milestone,
    "active_planning_intent": h_planning,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "gbp_unverified": h_gbp_unverified,
    "competitor_opened": h_competitor,
    "weather_heatwave": h_weather,
    "weather_alert": h_weather,
    "local_news_event": h_news,
}

CUSTOMER_HANDLERS = {
    "recall_due": h_recall_due,
    "customer_lapsed_hard": h_customer_winback,
    "customer_lapsed_soft": lambda c: h_customer_winback(c, hard=False),
    "winback_eligible": h_customer_winback,
    "trial_followup": h_trial_followup,
    "chronic_refill_due": h_refill,
    "wedding_package_followup": h_bridal,
    "appointment_tomorrow": h_appointment_tomorrow,
}

TABOO_FALLBACK = ["guaranteed", "100% safe", "miracle", "best in city", "cure"]


def _scrub(body: str, category: dict) -> str:
    taboos = list(((category.get("voice") or {}).get("vocab_taboo") or [])) + TABOO_FALLBACK
    for t in taboos:
        t = re.sub(r"\s*\(.*\)$", "", str(t)).strip()
        if t and len(t) > 3:
            body = re.sub(r"(?<![\w])" + re.escape(t) + r"(?![\w])", "", body, flags=re.I)
    body = re.sub(r"https?://\S+", "", body)       # no URLs (Meta rejects)
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    return body


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
            now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    ctx = Ctx(category or {}, merchant or {}, trigger or {}, customer, now)
    kind = ctx.kind
    is_customer = bool(customer) and (trigger.get("scope") == "customer" or trigger.get("customer_id"))
    skip = None
    if is_customer:
        skip = _consent_ok(ctx)
        handler = CUSTOMER_HANDLERS.get(kind, h_customer_generic)
    else:
        handler = MERCHANT_HANDLERS.get(kind, h_generic)
    d = handler(ctx)
    body = _scrub(d.body, ctx.category)
    send_as = "merchant_on_behalf" if is_customer else "vera"
    prefix = "merchant" if is_customer else "vera"
    return {
        "body": body,
        "cta": d.cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": d.rationale,
        "template_name": f"{prefix}_{kind}_v1",
        "template_params": [str(x) for x in (d.template_params or [])],
        "_offer": d.offer,
        "_deliverable": d.deliverable,
        "_skip": skip or d.skip,
    }
