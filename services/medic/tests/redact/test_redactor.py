"""K2 (minimal): the pattern-only redactor against the contract fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.medic.redact import REDACTION_VERSION, Redactor
from services.medic.redact.secret_names import NAMES

MEDIC = Path(__file__).resolve().parents[2]
CASES = json.loads((MEDIC / "contracts/fixtures/redaction/cases.json").read_text())
REPO = MEDIC.parents[1]

REDACTOR = Redactor()


def test_every_redact_awk_family_has_a_case() -> None:
    families = {c["family"] for c in CASES["must_redact"]}
    assert families == {
        "key_name",
        "url_credential",
        "pem",
        "token_shape",
        "flag",
        "contextual_token",
    }


@pytest.mark.parametrize("case", CASES["must_redact"], ids=lambda c: c["id"])
def test_must_redact(case: dict) -> None:
    out, hits = REDACTOR.redact(case["input"])
    assert case["secret"] not in out
    assert out == case["expected"]
    assert hits >= 1


@pytest.mark.parametrize("line", CASES["must_keep"])
def test_must_keep(line: str) -> None:
    assert REDACTOR.redact(line) == (line, 0)


def test_replacement_is_fixed_text_with_no_length_or_fragment() -> None:
    short, _ = REDACTOR.redact("password=abcdefgh")
    long, _ = REDACTOR.redact("password=" + "x" * 300)
    assert short == long == "password=[REDACTED]"


def test_already_redacted_text_is_stable() -> None:
    once, _ = REDACTOR.redact("GET /reset?token=abc123def456 token zzz999yyy888")
    assert REDACTOR.redact(once) == (once, 0)


def test_version_is_schema_safe() -> None:
    import re

    assert re.fullmatch(r"[a-z0-9.-]{1,32}", REDACTION_VERSION)


def test_secret_names_copy_matches_the_support_bundle_list() -> None:
    # Medic can't read scripts/ at runtime (its image ships services/medic/ only),
    # so it keeps a copy; this pins the copy to the original while both live here.
    theirs = REPO / "scripts/vigil-support/secret-names.txt"
    if not theirs.exists():
        pytest.skip("not in the Vigil repo")
    assert list(NAMES) == [
        n.strip() for n in theirs.read_text().splitlines() if n.strip()
    ]


def test_redact_ships_only_python() -> None:
    # The image build drops *.txt (.dockerignore); a data file here would be
    # missing at runtime and Medic would die at import.
    assert {p.suffix for p in (MEDIC / "redact").iterdir() if p.is_file()} == {".py"}
