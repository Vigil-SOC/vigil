from typing import Any, Dict, List


def extract_iocs(analysis: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return deduped, sorted IOCs keyed by ``CaseIOC.ioc_type``."""
    iocs: Dict[str, set] = {
        "ip": set(),
        "domain": set(),
        "url": set(),
        "hash": set(),
        "mutex": set(),
    }

    network = analysis.get("network") or {}
    for host in network.get("hosts") or []:
        ip = host.get("ip") if isinstance(host, dict) else host
        if ip and isinstance(ip, str):
            iocs["ip"].add(ip)

    for dns in network.get("dns") or []:
        req = dns.get("request") if isinstance(dns, dict) else None
        if req:
            iocs["domain"].add(req)

    for http in network.get("http") or []:
        u = http.get("uri") if isinstance(http, dict) else None
        if u:
            iocs["url"].add(u)

    for dropped in analysis.get("dropped") or []:
        if isinstance(dropped, dict):
            sha = dropped.get("sha256") or dropped.get("sha1") or dropped.get("md5")
            if sha:
                iocs["hash"].add(sha)

    summary = (analysis.get("behavior") or {}).get("summary") or {}
    for summary_key in ("mutexes", "mutex"):
        for mx in summary.get(summary_key) or []:
            iocs["mutex"].add(mx)

    return {k: sorted(v) for k, v in iocs.items()}
