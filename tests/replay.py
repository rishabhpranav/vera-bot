"""Replay scenarios for multi-turn behaviour against a running bot (run harness.py first)."""
import json, os, urllib.request
BOT = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
def post(path, body):
    req = urllib.request.Request(BOT + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
def convo(title, conv, mid, msgs, cust=None, role="merchant"):
    print(f"\n===== {title} ({conv})")
    for i, m in enumerate(msgs):
        r = post("/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": cust, "from_role": role,
                                "message": m, "received_at": "2026-04-26T10:42:00Z", "turn_number": i + 2})
        print(f"  {role.upper()}: {m}\n  BOT[{r['action']}]: {r.get('body') or r.get('wait_seconds') or ''}  <{r.get('cta','')}>\n     why: {r['rationale']}")
        if r["action"] == "end":
            break
M = "m_001_drmeera_dentist_delhi"
auto = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
convo("auto-reply hell (same conv)", "conv_m_001_drmeera_dentist_delhi_trg_022_cde_webinar_dentists", M, [auto]*4)
# simulator style: different conv ids each turn, same merchant
for i in range(1, 5):
    convo("simulator auto-reply", f"conv_auto_{i}", "m_003_studio11_salon_hyderabad", ["Thank you for contacting us! Our team will respond shortly."])
convo("intent transition", "conv_m_001_drmeera_dentist_delhi_trg_001_research_digest_dentists", M,
      ["Interesting. Is this relevant for my younger patients too?", "Hmm ok, what exactly will the patient message say?", "Ok, let's do it. What's next?", "CONFIRM"])
convo("simulator intent", "conv_intent_1", M, ["Ok lets do it. Whats next?"])
convo("hostile then GST", "conv_m_006_southindiancafe_restaurant_bangalore_trg_012_milestone_mylari", "m_006_southindiancafe_restaurant_bangalore",
      ["This is useless, you people keep bothering me", "can you also help me file my GST?", "ok fine send the review message"])
convo("simulator hostile", "conv_hostile", M, ["Stop messaging me. This is useless spam."])
convo("curveball GST (hinglish)", "conv_m_002_bharat_dentist_mumbai_trg_004_perf_dip_bharat", "m_002_bharat_dentist_mumbai",
      ["Btw GST filing mein help kar sakte ho kya is mahine?", "haan theek hai kar do", "confirm"])
convo("price question", "conv_m_002_bharat_dentist_mumbai_trg_005_renewal_due_bharat", "m_002_bharat_dentist_mumbai",
      ["How much is the renewal?", "busy right now, tomorrow"])
convo("customer slot pick", "conv_c_001_priya_for_m001_trg_003_recall_due_priya", M, ["2"], cust="c_001_priya_for_m001", role="customer")
convo("customer stop", "conv_c_010_rashmi_for_m007_trg_015_winback_rashmi", "m_007_powerhouse_gym_bangalore", ["please stop messaging"], cust="c_010_rashmi_for_m007", role="customer")
convo("merchant hard no", "conv_x", "m_009_apollo_pharmacy_jaipur", ["No thanks"])
convo("planning yes", "conv_m_008_zenyoga_gym_chennai_trg_016_kids_yoga_program_drafting", "m_008_zenyoga_gym_chennai",
      ["Looks good! Can we make it ages 5-10 instead?", "yes go ahead", "thanks!"])
