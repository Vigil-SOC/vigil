import json
from core.threat_intel.occurrence_rollup import occurrence_rollup
class Svc:
    def is_using_database(self): return False
    def get_findings(self, exclusions): return [
        {"severity": "high", "mitre_predictions": {"T1573.001": 0.9, "T1566": 0.8, "T9999": 0.7}},
        {"severity": "low", "mitre_predictions": {"T1021.001": 0.9}},
    ]
for t in occurrence_rollup(min_confidence=0, time_range="all", service=Svc())["techniques"]:
    print(json.dumps({k: t[k] for k in ("technique_id", "technique_name", "tactic")}))
