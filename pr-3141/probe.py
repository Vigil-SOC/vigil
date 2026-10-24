import asyncio
from unittest.mock import patch
from services.daemon.sandbox_poller import SandboxPoller
from core.cases import sandbox_correlation_service as c
from services.daemon.sandbox_submitter import SandboxSubmitter, SandboxSettings
import random

class DS:
    calls = 0
    def get_findings(self):
        DS.calls += 1
        raise TypeError("boom")
p = SandboxPoller(DS()); p._correlation = object()
print("run_once TypeError ->", asyncio.run(p.run_once()), "calls", DS.calls)
p2 = SandboxPoller(None); p2._init_services = lambda: None
print("run_once no data service ->", asyncio.run(p2.run_once()))

async def tag(name):
    async def f(t): return name
    return f
for meth in ("_fetch_cape","_fetch_hybrid","_fetch_anyrun","_fetch_joe"):
    async def f(t, m=meth): return m
    setattr(p, meth, f)
for n in ("cape","hybrid_analysis","anyrun","joe_sandbox","cape-sandbox","made-up"):
    print("fetch", n, "->", asyncio.run(p._fetch_report(n, "1")))
with patch.multiple(c, _normalise_cape=lambda r:"cape", _normalise_joe=lambda r:"joe",
                    _normalise_hybrid=lambda r:"hybrid", _normalise_anyrun=lambda r:"anyrun"):
    for n in ("cape","hybrid_analysis","anyrun","joe_sandbox","made-up"):
        print("normalise", n, "->", c._normalise_report(n, {}))

s = SandboxSubmitter(SandboxSettings(True, 50, ["exe"], True, True, True, True))
bad = 0
for _ in range(20000):
    h = "".join(random.choice("0123456789abcdefABCDEF g") for _ in range(random.choice([31,32,33,40,64,65])))
    if s.is_hash_safe_to_submit(h) and s._hash_type(h) is None: bad += 1
for h in ["a"*32+" ", " "+"a"*40, "A"*64]:
    print(repr(h), s.is_hash_safe_to_submit(h), s._hash_type(h))
print("safe hashes with no hash_type:", bad)
