"""In-memory state: versioned contexts, conversations, per-merchant flags."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str | None
    customer_id: str | None = None
    trigger_id: str | None = None
    kind: str = "generic"
    send_as: str = "vera"
    topic: str = ""
    offer: str = ""
    deliverable: str = ""
    stage: str = "pitched"          # pitched -> drafted -> done -> ended
    bot_bodies: list = field(default_factory=list)
    inbound: list = field(default_factory=list)
    auto_count: int = 0
    hostile_count: int = 0
    offtopic_count: int = 0
    question_count: int = 0
    nudges: int = 0
    lang_hinglish: bool | None = None
    ended: bool = False
    created: float = field(default_factory=time.time)


@dataclass
class MerchantFlags:
    opted_out: bool = False
    backoff_until: str | None = None   # ISO timestamp (simulated clock)
    auto_texts: list = field(default_factory=list)
    auto_count: int = 0
    last_sent_at: str | None = None


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.time()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}
            self.alias: dict[tuple[str, str], str] = {}     # (scope, payload-id) -> context_id
            self.convs: dict[str, Conversation] = {}
            self.flags: dict[str, MerchantFlags] = {}
            self.used_suppression: set[str] = set()
            self.sent_triggers: set[str] = set()

    # ------------------------------------------------------------ contexts
    def put(self, scope: str, cid: str, version: int, payload: dict):
        with self.lock:
            cur = self.contexts.get((scope, cid))
            if cur and cur["version"] >= version:
                return False, cur["version"]
            self.contexts[(scope, cid)] = {"version": version, "payload": payload, "ts": time.time()}
            pid = None
            if scope == "merchant":
                pid = payload.get("merchant_id")
            elif scope == "customer":
                pid = payload.get("customer_id")
            elif scope == "trigger":
                pid = payload.get("id")
            elif scope == "category":
                pid = payload.get("slug")
            if pid and pid != cid:
                self.alias[(scope, pid)] = cid
            return True, version

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        with self.lock:
            e = self.contexts.get((scope, cid))
            if not e:
                real = self.alias.get((scope, cid))
                if real:
                    e = self.contexts.get((scope, real))
            return e["payload"] if e else None

    def counts(self) -> dict:
        with self.lock:
            c = {s: 0 for s in SCOPES}
            for (scope, _cid) in self.contexts:
                c[scope] = c.get(scope, 0) + 1
            return c

    def triggers_for_merchant(self, merchant_id: str) -> list[dict]:
        with self.lock:
            out = []
            for (scope, _), e in self.contexts.items():
                if scope == "trigger" and e["payload"].get("merchant_id") == merchant_id:
                    out.append(e["payload"])
            return out

    def flag(self, merchant_id: str | None) -> MerchantFlags:
        key = merchant_id or "_unknown"
        with self.lock:
            if key not in self.flags:
                self.flags[key] = MerchantFlags()
            return self.flags[key]


STORE = Store()


# The API-facing store is STORE. The chat page uses its own Store so demo chats never
# touch data pushed through the API; handlers read whichever store is active via S().
from contextvars import ContextVar

_active: ContextVar = ContextVar("vera_store", default=None)


def S() -> Store:
    return _active.get() or STORE


def use_store(store: Store):
    return _active.set(store)


def reset_store(token):
    _active.reset(token)
