"""Dev-mode rule loading goes through rule_check, and its error codes surface as is."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.medic.contracts import rule_check
from services.medic.engine import RuleLoadError, load_rule, load_rule_file
from services.medic.tests.engine.runner import CONTRACTS

RULES = CONTRACTS / "fixtures" / "rules"
VALID = sorted((RULES / "valid").glob("*.yaml"))
INVALID = sorted((RULES / "invalid").glob("*.yaml"))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_rule_loads_with_derived_trust(path: Path) -> None:
    rule = load_rule_file(path)
    expected = rule_check.check_text(path.read_text())
    assert rule.input_trust == expected.input_trust
    assert rule.detection == expected.detection
    assert rule.skipped is None


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_rule_is_refused_with_the_loader_codes(path: Path) -> None:
    with pytest.raises(RuleLoadError) as exc:
        load_rule_file(path)
    expected = rule_check.check_text(path.read_text()).errors
    assert expected and exc.value.errors == expected
    assert exc.value.codes == {code for code, _ in expected}


def test_oversized_file_is_e_size(tmp_path: Path) -> None:
    big = tmp_path / "big.yaml"
    big.write_text("# " + "x" * 17_000)
    with pytest.raises(RuleLoadError) as exc:
        load_rule_file(big)
    assert exc.value.codes == {"E-SIZE"}


def _rule(**over) -> dict:
    import yaml

    rule = yaml.safe_load((RULES / "valid" / "ingest-source-silent.yaml").read_text())
    rule.update(over)
    return rule


def test_site_param_overrides_the_default() -> None:
    rule = load_rule(_rule(), params={"quiet_window": "1h"})
    assert rule.param("quiet_window") == 3600
    assert load_rule(_rule()).param("quiet_window") == 6 * 3600


def test_param_override_outside_its_bounds_is_e_param() -> None:
    with pytest.raises(RuleLoadError) as exc:
        load_rule(_rule(), params={"quiet_window": "30m"})
    assert exc.value.codes == {"E-PARAM"}
    with pytest.raises(RuleLoadError) as exc:
        load_rule(_rule(), params={"nope": 1})
    assert exc.value.codes == {"E-PARAM"}


def test_rule_needing_a_newer_minor_is_skipped_not_loaded_as_healthy() -> None:
    rule = load_rule(_rule(engine_api="1.7"))
    assert rule.skipped == "needs engine API 1.7, have 1.0"
