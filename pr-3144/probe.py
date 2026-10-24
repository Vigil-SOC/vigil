import re, time
from core.threat_intel.threat_feed_service import _extract_observables
naive = re.compile(r"([\w:.'-]+)\s*(?<![!<>])=\s*'((?:[^'\\]|\\.)*)'")
p = r"[url:value MATCHES 'x ipv4-addr:value = \'1.2.3.4\'' OR domain-name:value = 'kept.example']"
print("issue regex:", [m.groups() for m in naive.finditer(p)])
print("fix:", list(_extract_observables(p)))
for name, s in {
    "long path no op": "a:" + "b." * 50000 + "c",
    "unterminated quote": "'" + "\\a" * 50000,
    "many quotes in path": "a:b" + ".'x'" * 50000,
    "path then space flood": "a:b" + " " * 100000 + "x",
    "brackets": "a:b" + "[1]" * 50000,
}.items():
    t = time.perf_counter(); r = list(_extract_observables(s)); print(f"{name}: {len(s)} chars {time.perf_counter()-t:.3f}s -> {r}")
p = "[url:value MATCHES 'k = ' AND ipv4-addr:value = '1.2.3.4']"
print("MATCHES case issue regex:", [m.groups() for m in naive.finditer(p)])
print("MATCHES case fix:", list(_extract_observables(p)))
