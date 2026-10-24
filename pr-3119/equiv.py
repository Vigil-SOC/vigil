import subprocess, types, math
old_src = subprocess.check_output(["git","show","HEAD:core/integrations/darktrace/ingestion.py"], text=True)
old = types.ModuleType("old"); exec(compile(old_src, "old", "exec"), old.__dict__)
import core.integrations.darktrace.ingestion as new
inputs = [None, "junk", "", -5, -0.1, 0, 0.0, 0.3, 0.999, 1, 1.0, 1.0001, 2, 50, 99.9, 100, 150, "0.7", "80", float("inf"), True]
bad = [(x, old._score_to_anomaly(x), new._score_to_anomaly(x)) for x in inputs
       if not (old._score_to_anomaly(x) == new._score_to_anomaly(x))]
print("heuristic path mismatches:", bad)
svc_o, svc_n = old.DarktraceIngestionService(), new.DarktraceIngestionService()
for s in [0.8, 80, None]:
    a = svc_o.transform_model_breach({"pbid": 1, "score": s}); b = svc_n.transform_model_breach({"pbid": 1, "score": s})
    print("model_breach", s, a["anomaly_score"], a["severity"], "->", b["anomaly_score"], b["severity"])
for gs in [0, 0.5, 1, 2, 92, 100, 150, "junk"]:
    a = svc_o.transform_ai_analyst({"uuid": "u", "groupScore": gs}); b = svc_n.transform_ai_analyst({"uuid": "u", "groupScore": gs})
    print("ai_analyst groupScore", repr(gs), a["anomaly_score"], a["severity"], "->", b["anomaly_score"], b["severity"])
assert not bad
