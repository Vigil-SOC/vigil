"""K2/K1 T-03: Medic's own log output goes through the same redactor."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from services.medic.redact.choke import WITHHELD, install_log_redaction
from services.medic.redact.rules import Redactor

MEDIC = Path(__file__).resolve().parents[2]
CASES = json.loads((MEDIC / "contracts/fixtures/redaction/cases.json").read_text())[
    "must_redact"
]


@pytest.fixture
def output() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("services.medic")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    handle = install_log_redaction()
    try:
        yield stream
    finally:
        handle.uninstall()
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_canary_per_family_absent_from_medic_log(output: io.StringIO) -> None:
    log = logging.getLogger("services.medic.sensors.test")
    for case in CASES:
        log.warning("read failed: %s", case["input"])
        log.debug(case["input"])  # pre-rendered, no args
    text = output.getvalue()
    for case in CASES:
        assert case["secret"] not in text, case["id"]
    assert text.count("[REDACTED]") >= 2 * len(CASES)


def test_traceback_text_is_redacted(output: io.StringIO) -> None:
    secret = "canaryTraceback0021abc"
    try:
        raise RuntimeError(f"connect redis://default:{secret}@redis:6379 failed")
    except RuntimeError:
        logging.getLogger("services.medic").exception("sensor crashed")
    text = output.getvalue()
    assert "RuntimeError" in text and "Traceback" in text
    assert secret not in text


def test_redactor_failure_withholds_the_record(
    output: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Redactor, text: str) -> tuple[str, int]:
        raise RuntimeError("redactor bug")

    monkeypatch.setattr(Redactor, "redact", boom)
    logging.getLogger("services.medic").error("password=%s", "canaryWithheld0022")
    text = output.getvalue()
    assert "canaryWithheld0022" not in text
    assert WITHHELD in text


def test_medic_code_passes_no_extra_to_loggers() -> None:
    # `extra` fields are attached after the record factory runs, so they would
    # skip redaction. Medic logs through the message only. A local venv
    # (`uv run` inside services/medic) is third-party code, not Medic's.
    offenders = [
        str(p.relative_to(MEDIC))
        for p in MEDIC.rglob("*.py")
        if "tests" not in p.parts
        and "contracts" not in p.parts
        and ".venv" not in p.parts
        and "site-packages" not in p.parts
        and "extra=" in p.read_text()
    ]
    assert offenders == []
