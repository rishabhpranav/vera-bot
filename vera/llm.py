"""Optional LLM layer (Anthropic or OpenAI-compatible) with strict guards.

The bot is fully functional without an LLM. When a key is configured the LLM is
used to (a) polish proactive drafts and (b) answer free-form merchant questions.
Every LLM output is validated; on any failure the deterministic text is used.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

PROVIDER = os.getenv("LLM_PROVIDER", "").lower()
API_KEY = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
if not PROVIDER and API_KEY:
    PROVIDER = "anthropic" if (os.getenv("ANTHROPIC_API_KEY") or API_KEY.startswith("sk-ant")) else "openai"
MODEL = os.getenv("LLM_MODEL") or ("claude-sonnet-5" if PROVIDER == "anthropic" else "gpt-4o-mini")
BASE_URL = os.getenv("LLM_BASE_URL", "")  # for OpenAI-compatible hosts (Groq, OpenRouter, DeepSeek…)
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "9"))
POLISH_TICK = os.getenv("LLM_POLISH_TICK", "1") == "1"


def enabled() -> bool:
    return bool(API_KEY) and PROVIDER in ("anthropic", "openai") and os.getenv("LLM_DISABLED") != "1"


def model_label() -> str:
    return MODEL if enabled() else "deterministic-composer (no LLM)"


def _call(system: str, user: str, max_tokens=500) -> str:
    if PROVIDER == "anthropic":
        req = urllib.request.Request(
            (BASE_URL or "https://api.anthropic.com") + "/v1/messages",
            data=json.dumps({"model": MODEL, "max_tokens": max_tokens, "temperature": 0,
                             "system": system, "messages": [{"role": "user", "content": user}]}).encode(),
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    req = urllib.request.Request(
        (BASE_URL or "https://api.openai.com") + "/v1/chat/completions",
        data=json.dumps({"model": MODEL, "max_tokens": max_tokens, "temperature": 0,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]}).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM_RE.findall(s or "")}


def validate(text: str, grounding: str, taboos: list[str]) -> str | None:
    if not text:
        return None
    text = text.strip().strip('"').strip()
    if not text or len(text) > 1100:
        return None
    if re.search(r"https?://|www\.", text, re.I):
        return None
    low = text.lower()
    for t in taboos or []:
        t = re.sub(r"\s*\(.*\)$", "", str(t)).strip().lower()
        if t and re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", low):
            return None
    allowed = _numbers(grounding)
    for n in _numbers(text):
        if n not in allowed and not (len(n) <= 1):  # single digits (1/2 reply options) are fine
            return None
    return text


SYSTEM_BASE = """You are Vera, a WhatsApp growth assistant for small Indian businesses (clinics, salons, gyms, restaurants, pharmacies).
Rules:
- Use ONLY facts present in the provided context/draft. Never invent numbers, names, offers, dates, studies or competitors.
- Peer/colleague tone matching the category voice; no hype, no preamble, never re-introduce yourself.
- If the merchant prefers Hindi-English, write natural Hinglish (Roman script). Otherwise English.
- Exactly one call to action, placed in the last sentence. No URLs.
- Keep it WhatsApp-short (under ~90 words unless delivering a requested draft).
Output ONLY the message text."""


def polish(draft: str, facts: dict, voice: dict, hinglish: bool, taboos: list[str]) -> str | None:
    if not enabled() or not POLISH_TICK:
        return None
    user = (f"Category voice: {json.dumps(voice, ensure_ascii=False)[:600]}\n"
            f"Language: {'Hinglish' if hinglish else 'English'}\n"
            f"Context facts (JSON): {json.dumps(facts, ensure_ascii=False)[:3500]}\n\n"
            f"Draft message:\n{draft}\n\n"
            "Rewrite the draft to be sharper and more compelling for this specific merchant. Keep every number, "
            "date, price and source from the draft exactly; you may drop weaker sentences. Keep the same CTA.")
    try:
        out = _call(SYSTEM_BASE, user)
    except Exception:
        return None
    return validate(out, draft + json.dumps(facts, ensure_ascii=False), taboos)


def answer(question: str, history: list[dict], facts: dict, offer: str, hinglish: bool, taboos: list[str]) -> str | None:
    if not enabled():
        return None
    user = (f"Language: {'Hinglish' if hinglish else 'English'}\n"
            f"Context facts (JSON): {json.dumps(facts, ensure_ascii=False)[:3500]}\n"
            f"Conversation so far: {json.dumps(history[-8:], ensure_ascii=False)}\n"
            f"What Vera has offered to do: {offer}\n\n"
            f"Merchant's latest message: {question}\n\n"
            "Reply as Vera. Answer the question directly using only the facts. If the answer isn't in the facts, say "
            "you'll confirm rather than guess. End with one low-friction next step toward the offer.")
    try:
        out = _call(SYSTEM_BASE, user, max_tokens=400)
    except Exception:
        return None
    grounding = json.dumps(facts, ensure_ascii=False) + json.dumps(history, ensure_ascii=False) + question + offer
    return validate(out, grounding, taboos)
