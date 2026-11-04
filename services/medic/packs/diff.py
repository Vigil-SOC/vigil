"""pack_diff (F5): what changed between two medic-core pack sources, and the minimum
version bump it needs.

    python -m services.medic.packs.diff <old_dir> <new_dir>

Prints a markdown report grouped by level; its last line is `minimum bump: LEVEL`
(MAJOR | MINOR | PATCH | none). Exits 1 when the new pack.yaml version is below that
bump, 2 when a source can't be read. CI runs it against the last medic-core-v* tag
(or the merge base) and writes the report to the job summary.

Levels: MAJOR when the engine_api MAJOR changes; MINOR for a rule added or removed,
a rule's behaviour changing (anything but advice text and param bounds), suppression,
catalog, the Vigil range, the engine_api MINOR or a runbook added or removed; PATCH
for advice text, param defaults/bounds, runbook wording or an expiry refresh. A rule
`revision` alone is not a level. Packs stay 0.x, so while the baseline's major is 0 a
MAJOR change needs a MINOR bump (0.Y+1.0), not 1.0.0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from services.medic.contracts import rule_check
from services.medic.contracts.pack_check import _semver, _yaml

LEVELS = ["PATCH", "MINOR", "MAJOR"]
# Rule keys that never move the level on their own; params is compared separately.
_RULE_SOFT = {"revision", "advice", "params"}
# pack.yaml keys that are not behaviour: the version is what the gate checks, the
# timestamps are the expiry refresh.
_PACK_SOFT = {"version", "created_at", "expires_at"}


def _load(path: Path):
    return _yaml(path.read_text())


def _rules(root: Path) -> dict[str, dict]:
    out = {}
    for f in sorted((root / "rules").glob("*.yaml")):
        doc, res = rule_check.load(f.read_text())
        if doc is None:
            raise ValueError(f"{f}: {res.errors[0][1]}")
        out[doc["id"]] = doc
    return out


def _runbooks(root: Path) -> dict[str, bytes]:
    base = root / "runbooks"  # absent today
    if not base.is_dir():
        return {}
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def _opt(root: Path, name: str):
    return _load(root / name) if (root / name).is_file() else None


def _rule_changes(rid: str, old: dict, new: dict) -> list[tuple[str, str]]:
    out = []
    keys = sorted(
        k
        for k in old.keys() | new.keys()
        if k not in _RULE_SOFT and old.get(k) != new.get(k)
    )
    if keys:
        out.append(("MINOR", f"rule `{rid}` changed: {', '.join(keys)}"))
    op, np_ = old.get("params") or {}, new.get("params") or {}
    if {k: v.get("type") for k, v in op.items()} != {
        k: v.get("type") for k, v in np_.items()
    }:
        out.append(("MINOR", f"rule `{rid}` params added, removed or retyped"))
    elif op != np_:
        out.append(("PATCH", f"rule `{rid}` param defaults or bounds changed"))
    if old.get("advice") != new.get("advice"):
        out.append(("PATCH", f"rule `{rid}` advice text changed"))
    return out


def _rest(d: dict) -> dict:
    """pack.yaml minus what has its own level."""
    return {
        **{k: v for k, v in d.items() if k not in {"pack", "engine_api", "vigil"}},
        **{f"pack.{k}": v for k, v in d["pack"].items() if k not in _PACK_SOFT},
    }


def _pack_changes(old: dict, new: dict) -> list[tuple[str, str]]:
    out = []
    oa, na = (tuple(int(x) for x in d["engine_api"].split(".")) for d in (old, new))
    if oa[0] != na[0]:
        out.append(
            ("MAJOR", f"engine_api MAJOR {old['engine_api']} → {new['engine_api']}")
        )
    elif oa != na:
        out.append(
            ("MINOR", f"engine_api MINOR {old['engine_api']} → {new['engine_api']}")
        )
    if old.get("vigil") != new.get("vigil"):
        out.append(("MINOR", f"Vigil range {old.get('vigil')} → {new.get('vigil')}"))
    ro, rn = _rest(old), _rest(new)
    if ro != rn:
        keys = sorted(k for k in ro.keys() | rn.keys() if ro.get(k) != rn.get(k))
        out.append(("MINOR", f"pack.yaml changed: {', '.join(keys)}"))
    if any(
        old["pack"].get(k) != new["pack"].get(k) for k in ("created_at", "expires_at")
    ):
        out.append(("PATCH", "expiry refreshed (pack.created_at / expires_at)"))
    return out


def diff(old: Path, new: Path) -> list[tuple[str, str]]:
    """Every change between two pack source directories, as (level, description)."""
    out = _pack_changes(_load(old / "pack.yaml"), _load(new / "pack.yaml"))
    ro, rn = _rules(old), _rules(new)
    out += [("MINOR", f"rule added: `{r}`") for r in sorted(rn.keys() - ro.keys())]
    out += [("MINOR", f"rule removed: `{r}`") for r in sorted(ro.keys() - rn.keys())]
    for rid in sorted(ro.keys() & rn.keys()):
        out += _rule_changes(rid, ro[rid], rn[rid])
    for name in ("suppression.yaml", "catalog.yaml"):
        if _opt(old, name) != _opt(new, name):
            out.append(("MINOR", f"`{name}` changed"))
    bo, bn = _runbooks(old), _runbooks(new)
    out += [("MINOR", f"runbook added: `{r}`") for r in sorted(bn.keys() - bo.keys())]
    out += [("MINOR", f"runbook removed: `{r}`") for r in sorted(bo.keys() - bn.keys())]
    out += [
        ("PATCH", f"runbook wording changed: `{r}`")
        for r in sorted(bo.keys() & bn.keys())
        if bo[r] != bn[r]
    ]
    return out


def required(
    changes: list[tuple[str, str]], base_version: str
) -> tuple[str | None, str | None]:
    """(minimum bump, note). Level None means no change needs a bump."""
    top = max((c[0] for c in changes), key=LEVELS.index, default=None)
    if top == "MAJOR" and _semver(base_version)[0] == 0:
        return "MINOR", "a MAJOR change is a MINOR bump while the pack is 0.x"
    return top, None


def floor(base_version: str, level: str | None) -> tuple[int, int, int]:
    """The lowest version that satisfies the bump."""
    major, minor, patch = _semver(base_version)
    return {
        "MAJOR": (major + 1, 0, 0),
        "MINOR": (major, minor + 1, 0),
        "PATCH": (major, minor, patch + 1),
        None: (major, minor, patch),
    }[level]


def report(old: Path, new: Path) -> tuple[str, bool]:
    """(markdown, ok): ok is False when the new version is below the required bump."""
    ov, nv = (_load(d / "pack.yaml")["pack"]["version"] for d in (old, new))
    changes = diff(old, new)
    level, note = required(changes, ov)
    lines = ["## medic-core pack diff", ""]
    for lv in reversed(LEVELS):
        group = [d for c, d in changes if c == lv]
        if group:
            lines += [f"### {lv}", *(f"- {d}" for d in group), ""]
    if not changes:
        lines += ["No changes that need a version bump.", ""]
    ok = _semver(nv) >= floor(ov, level)
    need = ".".join(map(str, floor(ov, level)))
    lines.append(
        f"pack.yaml version {ov} → {nv}: "
        + ("ok." if ok else f"**too low**, the changes above need {need} or higher.")
    )
    if note:
        lines.append(f"Downgraded from MAJOR: {note}.")
    lines += ["", f"minimum bump: {level or 'none'}"]
    return "\n".join(lines), ok


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    try:
        text, ok = report(Path(argv[0]), Path(argv[1]))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"pack diff failed: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
