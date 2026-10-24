import sys
sys.path.insert(0, ".")
from core.threat_intel import threat_feed_service as feed
from services.daemon import orchestrator, threat_feed_poller as poller

rows = [
    {"indicator_type": "ip", "indicator_value": "203.0.113.7"},
    {"indicator_type": "ip", "indicator_value": "203.0.113[.]7"},
    {"indicator_type": "ip", "indicator_value": " 203.0.113.7 "},
    {"indicator_type": "domain", "indicator_value": "EVIL.example"},
    {"indicator_type": "domain", "indicator_value": "evil[.]example"},
]
feed._recent_indicators = lambda limit: rows
import core.memory.hunt_coverage as hc
hc.check_coverage = lambda entity_keys: {"status": "uncovered", "proposal": hc.build_proposal(entity_keys, []), "execute": None}
poller.is_enabled = lambda _wf: True
poller._intel_intake_state = lambda: poller._IntelIntake(False, set())
captured = []
orchestrator.insert_intake_trigger = lambda **kw: captured.append(kw) or 1

proposals = feed.propose_hunts_from_recent_indicators()["proposals"]
keys = [p["entity_key"] for p in proposals]
print("proposal keys:", keys)
assert len(keys) == len(set(keys)), "duplicate keys reach the poller"
result = poller.ThreatFeedPoller().offer_uncovered_indicators_to_intake()
print("offer result:", result)
subjects = captured[0]["payload"]["hypothesis_subjects"]
print("subjects:", subjects)
flat = [k for v in subjects.values() for k in v]
assert len(flat) == len(set(flat)), "duplicate keys in the intake row"
print("OK: no duplicate key reached the intake row")
