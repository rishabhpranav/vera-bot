"""Offline entry point: compose(category, merchant, trigger, customer) -> dict.

Also: `python bot.py <expanded_dataset_dir> [now_iso]` writes submission.jsonl for the 30 sample test pairs.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from vera.composer import compose as _compose
from vera.util import parse_dt

PUBLIC_KEYS = ("body", "cta", "send_as", "suppression_key", "rationale", "template_name", "template_params")


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, now=None) -> dict:
    out = _compose(category, merchant, trigger, customer, now or datetime.now(timezone.utc))
    return {k: out[k] for k in PUBLIC_KEYS}


if __name__ == "__main__":
    ds = Path(sys.argv[1] if len(sys.argv) > 1 else "expanded")
    now = parse_dt(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(timezone.utc)
    rd = lambda p: json.load(open(p))
    cats = {p.stem: rd(p) for p in (ds / "categories").glob("*.json")}
    with open("submission.jsonl", "w") as f:
        for pair in rd(ds / "test_pairs.json")["pairs"]:
            t = rd(ds / "triggers" / f"{pair['trigger_id']}.json")
            m = rd(ds / "merchants" / f"{pair['merchant_id']}.json")
            c = rd(ds / "customers" / f"{pair['customer_id']}.json") if pair.get("customer_id") else None
            r = compose(cats[m["category_slug"]], m, t, c, now)
            f.write(json.dumps({"test_id": pair["test_id"], **r}, ensure_ascii=False) + "\n")
    print("wrote submission.jsonl")
