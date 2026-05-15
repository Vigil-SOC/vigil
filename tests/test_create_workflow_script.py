"""Parity tests for ``scripts/create_workflow.py``'s agent list.

Background: ``AVAILABLE_AGENTS`` in ``scripts/create_workflow.py`` previously
dropped the ``auto_responder`` agent, so the 60-second workflow scaffolder
could not reference Vigil's autonomous-response capability. The fix
(``auto_responder`` re-added to the list) is paired with this test so a
future drift between the script's allow-list and ``AGENT_CONFIGS`` in
``services.soc_agents`` fails fast in CI.

See: https://github.com/Vigil-SOC/vigil/issues/204
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


def _load_script_module():
    """Import ``scripts/create_workflow.py`` as a module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "vigil_create_workflow", REPO / "scripts" / "create_workflow.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_auto_responder_is_in_available_agents():
    """Direct regression for the missing 13th agent (issue #204)."""
    mod = _load_script_module()
    assert "auto_responder" in mod.AVAILABLE_AGENTS, (
        "auto_responder must be selectable by scripts/create_workflow.py"
    )


def test_available_agents_matches_agent_configs_keys():
    """The script's allow-list must stay in lock-step with the canonical
    AGENT_CONFIGS dict in services.soc_agents. If they drift, new agents
    silently become unscaffoldable (or the script offers agents that don't
    exist). Fail fast here instead of debugging "unknown agent" at runtime.
    """
    mod = _load_script_module()
    from services.soc_agents import AGENT_CONFIGS

    script_agents = set(mod.AVAILABLE_AGENTS)
    config_agents = set(AGENT_CONFIGS.keys())

    missing_in_script = config_agents - script_agents
    extra_in_script = script_agents - config_agents

    assert not missing_in_script and not extra_in_script, (
        f"scripts/create_workflow.py:AVAILABLE_AGENTS has drifted from "
        f"services.soc_agents:AGENT_CONFIGS.\n"
        f"  Missing from script (defined in AGENT_CONFIGS but not selectable): "
        f"{sorted(missing_in_script) or 'none'}\n"
        f"  Extra in script (selectable but not defined in AGENT_CONFIGS): "
        f"{sorted(extra_in_script) or 'none'}"
    )


def test_available_agents_has_no_duplicates():
    """A name appearing twice would cause confusing UX in the script's
    epilog and validator messages."""
    mod = _load_script_module()
    duplicates = [a for a in mod.AVAILABLE_AGENTS if mod.AVAILABLE_AGENTS.count(a) > 1]
    assert not duplicates, f"AVAILABLE_AGENTS contains duplicates: {sorted(set(duplicates))}"


def test_build_template_renders_with_auto_responder():
    """The template renderer must accept auto_responder and embed it in
    the rendered YAML frontmatter + Phase section. Covers the rendering
    path the script's CLI uses, without subprocess side-effects on the
    real workflows/ directory.
    """
    mod = _load_script_module()
    rendered = mod.build_template("test-auto-responder-wf", ["auto_responder"])
    assert "auto_responder" in rendered, "rendered template omits auto_responder"
    assert "Auto Responder" in rendered, "rendered template missing Title-cased agent name"
