import re, sys
from core.threat_intel.mitre_lookup import resolve_technique
src = open("clients/web/src/data/mitre.ts").read()
rows = re.findall(r"'?(T[\d.]+)'?: \{ name: '([^']+)', tactic: '([^']+)' \}", src)
bad = 0
for tid, name, tactic in rows:
    _, n, t = resolve_technique(tid)
    flag = "" if (n, t) == (name, tactic) else "  <-- DIFF"
    bad += bool(flag)
    print(f"{tid:10} server=({n}, {t}) client=({name}, {tactic}){flag}")
print(f"{len(rows)} client ids, {bad} differ")
for tid in ["T1573.001", "T1048.003", "T1036", "T9999"]:
    print(tid, resolve_technique(tid))
