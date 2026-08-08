"""Rendering test for ``scripts/create_workflow.py``.

Background: ``AVAILABLE_AGENTS`` in ``scripts/create_workflow.py`` previously
dropped the ``auto_responder`` agent, so the 60-second workflow scaffolder
could not reference Vigil's autonomous-response capability. The list is now
derived from ``core.agents.builtins.AgentId`` (#476), so it cannot drift; what
remains worth testing is that the template renderer handles a real agent id.

See: https://github.com/Vigil-SOC/vigil/issues/204
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
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
