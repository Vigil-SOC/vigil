import re, subprocess, time
from core.threat_intel import threat_feed_service as m
src = subprocess.run(["git", "show", "f1cfb488:core/threat_intel/threat_feed_service.py"], capture_output=True, text=True).stdout
ns = {"re": re}
exec(src[src.index("_STIX_COMPARISON = re.compile("):src.index("_STIX_ESCAPE")], ns)
shapes = {
    "dashes": lambda n: "-" * n,
    "quote + \\' repeats": lambda n: "'" + "\\'" * n,
    "path + .' repeats": lambda n: "a:b" + ".'" * n,
    "a:b.'a:b.' repeats": lambda n: "a:b.'" * n,
    "words no colon": lambda n: "ab " * n,
    "path, spaces, no op": lambda n: "a:b" + " " * n,
    "a:b = '\\' repeats": lambda n: "a:b = '" + "\\'" * n,
}
for name, make in shapes.items():
    for label, rx in (("before", ns["_STIX_COMPARISON"]), ("after", m._STIX_COMPARISON)):
        ts = []
        for n in (5000, 50000):
            s = make(n); t = time.perf_counter(); list(rx.finditer(s)); ts.append(time.perf_counter() - t)
        print(f"{name:24} {label:6} n=5k {ts[0]:.4f}s n=50k {ts[1]:.4f}s ratio {ts[1]/max(ts[0],1e-6):.0f}x")
