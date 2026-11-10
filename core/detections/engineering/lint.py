"""The checks a rule must pass before it is replayed, each one reported on its own.

Vigil's ``lint_sigma`` refuses a rule keyed to one host, address or account. The rest is the
structure a reviewer needs, ported from the detection write-back tool. A check that could not
run says so with ``passed`` None, and only a blocking check that failed stops the rule.

Ported from 1.x (#4157) without its pySigma and log_events checks: main has neither. Whether
the SIEMs hold the fields the rule reads is the replay's to say.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from core.detections.engineering import sigma_text
from core.detections.lint import lint_sigma
from core.detections.replay import _compile_rule, _Unevaluable


@dataclass(frozen=True)
class Check:
    name: str
    passed: Optional[bool]
    blocking: bool
    detail: str = ""


def _structure(doc: Optional[Dict[str, Any]]) -> List[Check]:
    if doc is None:
        return [Check("Reads as one Sigma rule", False, True, "Not one YAML mapping.")]
    missing = [
        key
        for key, kind in (("title", str), ("logsource", dict), ("detection", dict))
        if not isinstance(doc.get(key), kind)
    ]
    detection = doc.get("detection")
    condition = detection.get("condition") if isinstance(detection, dict) else None
    return [
        Check(
            "Has a title, log source and detection",
            not missing,
            True,
            f"Missing or the wrong type: {', '.join(missing)}." if missing else "",
        ),
        Check(
            "The detection has a condition",
            bool(condition),
            True,
            "" if condition else "Sigma needs one to know what to match.",
        ),
    ]


def _advice(doc: Dict[str, Any]) -> List[Check]:
    return [
        Check(
            "Tagged with an ATT&CK technique",
            bool(sigma_text.techniques(doc)),
            False,
            "" if sigma_text.techniques(doc) else "The heat map will not count it.",
        ),
        Check(
            "False positives are listed",
            bool(doc.get("falsepositives")),
            False,
            "" if doc.get("falsepositives") else "Say what normal activity matches.",
        ),
        Check(
            "Has a description",
            bool(doc.get("description")),
            False,
        ),
    ]


def _replayable(text: str) -> Check:
    name = "Vigil can replay it"
    try:
        _compile_rule(text)
    except _Unevaluable as exc:
        return Check(name, False, False, f"The replay cannot evaluate {exc}.")
    except (ValueError, RecursionError) as exc:
        return Check(name, False, False, str(exc))
    return Check(name, True, False)


def lint_rule(text: str) -> Dict[str, Any]:
    """Every check on ``text``."""
    unsafe = sigma_text.unsafe_because(text)
    if unsafe:
        check = Check("Written out in full", False, True, unsafe)
        return {"passed": False, "checks": [asdict(check)], "findings": []}
    doc = sigma_text.parse(text)
    vigil = lint_sigma(rule_yaml=text)
    checks = [
        *_structure(doc),
        Check(
            "Not tied to one host, address or account",
            bool(vigil.get("passed")),
            True,
            vigil.get("rewrite_guidance") or vigil.get("error") or "",
        ),
    ]
    if doc is not None:
        checks += [*_advice(doc), _replayable(text)]
    return {
        "passed": all(c.passed is not False for c in checks if c.blocking),
        "checks": [asdict(c) for c in checks],
        "findings": vigil.get("findings") or [],
    }
