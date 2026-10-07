"""``shutdown_all.sh --full`` with ``-d`` deletes every compose data volume.

``dc down -v`` removes named volumes (the database, Bifrost, the Compose
State Directory, and the default backup repository). Help must say so and
exit before any process is killed, and the README Shutdown section must
say the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


def test_help_exits_zero_and_full_warns_before_down_v() -> None:
    text = (REPO / "shutdown_all.sh").read_text(encoding="utf-8")
    help_at = text.index("-h|--help)")
    unknown_at = text.index("*)")
    assert help_at < unknown_at < text.index("Stopping Vigil SOC...")

    help_arm = text[help_at:unknown_at]
    unknown_arm = text[unknown_at : text.index("esac", unknown_at)]
    assert "exit 0" in help_arm
    assert "exit 1" in unknown_arm

    lowered = help_arm.lower()
    full_at = lowered.index("--full")
    with_d = lowered.index("-d", full_at)
    permanent = lowered.index("permanent", with_d)
    assert "delet" in lowered[permanent:]

    full_branch = text.split('if [ "$FULL" -eq 1 ]; then', 1)[1]
    full_branch = full_branch.split("else", 1)[0]
    assert full_branch.index(">&2") < full_branch.index("dc down -v")


def test_readme_shutdown_section_marks_full_as_permanent_deletion() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    start = readme.index("### Shutdown")
    section = readme[start : readme.index("\n### ", start + 1)]
    for name in ("postgres_data", "bifrost_data", "vigil_home", "backup_repo"):
        assert name in section
    d_at = section.index("./shutdown_all.sh -d")
    full_at = section.index("./shutdown_all.sh -d --full")
    assert d_at < full_at
    line = section[full_at : section.index("\n", full_at)].lower()
    assert "permanent" in line
    assert "delet" in line
