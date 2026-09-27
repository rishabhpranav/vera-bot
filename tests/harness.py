"""Local end-to-end test: warmup, triggers, ticks, replay scenarios."""
import json, os, sys, time, glob, urllib.request, urllib.error
BOT = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
DS = os.getenv("DATASET", os.path.join(os.path.dirname(__file__), "..", "data"))
NOW = os.getenv("SIM_NOW", "2026-04-26T10:30:00Z")

def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BOT + path, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t

def load(sub):
    return [json.load(open(f)) for f in sorted(glob.glob(f"{DS}/{sub}/*.json"))]

def main():
    fails = []
    s, h, _ = call("GET", "/v1/healthz"); assert s == 200, h
    s, m, _ = call("GET", "/v1/metadata"); assert s == 200 and m["team_name"], m
    cats, mers, custs, trgs = load("categories"), load("merchants"), load("customers"), load("triggers")
    for c in cats: assert call("POST", "/v1/context", {"scope": "category", "context_id": c["slug"], "version": 1, "payload": c, "delivered_at": NOW})[0] == 200
    for x in mers: assert call("POST", "/v1/context", {"scope": "merchant", "context_id": x["merchant_id"], "version": 1, "payload": x, "delivered_at": NOW})[0] == 200
    for x in custs: assert call("POST", "/v1/context", {"scope": "customer", "context_id": x["customer_id"], "version": 1, "payload": x, "delivered_at": NOW})[0] == 200
    s, h, _ = call("GET", "/v1/healthz"); print("healthz after warmup:", h["contexts_loaded"])
    # idempotency
    s, r, _ = call("POST", "/v1/context", {"scope": "merchant", "context_id": mers[0]["merchant_id"], "version": 1, "payload": mers[0], "delivered_at": NOW})
    print("re-push same version ->", s, r); assert s == 409
    s, r, _ = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    print("bad scope ->", s, r); assert s == 400
    for t in trgs: assert call("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t, "delivered_at": NOW})[0] == 200
    s, h, _ = call("GET", "/v1/healthz"); print("healthz:", h["contexts_loaded"])
    out = []
    ids = [t["id"] for t in trgs]
    maxlat = 0
    for rnd in range(4):   # multiple ticks so deferred (same-merchant) triggers go out later
        s, r, lat = call("POST", "/v1/tick", {"now": NOW, "available_triggers": ids})
        maxlat = max(maxlat, lat)
        assert s == 200
        out += r["actions"]
        print(f"tick {rnd}: {len(r['actions'])} actions ({lat*1000:.0f} ms)")
    req = ["conversation_id", "merchant_id", "send_as", "trigger_id", "cta", "suppression_key", "rationale", "body", "template_name", "template_params"]
    for a in out:
        miss = [k for k in req if k not in a]
        if miss or not a["body"].strip(): fails.append(("malformed", a.get("trigger_id"), miss))
        if "http" in a["body"]: fails.append(("url", a["trigger_id"]))
    json.dump(out, open(os.getenv("OUT", "/tmp/actions.json"), "w"), indent=1, ensure_ascii=False)
    print("total actions:", len(out), "unique triggers:", len({a['trigger_id'] for a in out}), "max tick latency: %.0f ms" % (maxlat*1000))
    print("fails:", fails)

if __name__ == "__main__":
    main()
