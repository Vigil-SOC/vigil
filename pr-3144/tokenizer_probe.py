import time
from core.threat_intel.threat_feed_service import _extract_observables as f
print("unterminated:", list(f(r"[ipv4-addr:value = '1.2.3.4\']")))
print("no spaces:", list(f("[ipv4-addr:value='1.2.3.4']OR[domain-name:value='a.example']")))
print("sha256:", list(f("[file:hashes.'SHA-256' = 'abc']")))
print("index path:", list(f("[x:a[0] = 'v' AND url:value = 'u']")))
shapes = {"dashes": lambda n: "-"*n, "quote + \\' repeats": lambda n: "'"+"\\'"*n,
  "a:b.'a:b.' repeats": lambda n: "a:b.'"*n, "a:b = '\\' repeats": lambda n: "a:b = '"+"\\'"*n,
  "path, spaces, no op": lambda n: "a:b"+" "*n, "a:b.'aaa": lambda n: "a:b.'"+"a"*n,
  "= ' pairs": lambda n: "a:b = '" * n}
for name, make in shapes.items():
    ts=[]
    for n in (5000, 50000):
        s=make(n); t=time.perf_counter(); list(f(s)); ts.append(time.perf_counter()-t)
    print(f"{name:24} n=5k {ts[0]:.4f}s n=50k {ts[1]:.4f}s ratio {ts[1]/max(ts[0],1e-6):.0f}x")
