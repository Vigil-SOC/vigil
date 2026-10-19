"""Where restore puts backed-up skills when VIGIL_SKILLS_PATH is unset or empty.

Unset, the target is the State Directory's skills/ folder, which the State
Directory's own swap would move aside. Staging and swapping run on plain
directories, so no database or restic is needed here.
"""

import pytest

from core.backup.create import BackupError
from core.backup.restore import _stage_locations, _swap_files, _target_for
from core.config import Settings

STAMP = "20261013T000000Z"


def _snapshot(root):
    """An extracted snapshot from a host whose skills lived outside its state."""
    (root / "old/state").mkdir(parents=True)
    (root / "old/state/secrets.enc").write_text("restored secrets")
    (root / "old/skills/desk-check").mkdir(parents=True)
    (root / "old/skills/desk-check/SKILL.md").write_text("restored skill")
    return {
        "locations": [
            {"name": "state_directory", "status": "included", "path": "/old/state"},
            {"name": "skills", "status": "included", "path": "/old/skills"},
        ]
    }


def test_unset_path_restores_skills_into_the_state_directory(tmp_path, monkeypatch):
    state = tmp_path / "host/state"
    (state / "skills/live-skill").mkdir(parents=True)
    (state / "secrets.enc").write_text("live secrets")
    monkeypatch.setenv("VIGIL_DIR", str(state))
    monkeypatch.delenv("VIGIL_SKILLS_PATH", raising=False)
    manifest = _snapshot(tmp_path / "snap")
    placed, created = [], []

    _stage_locations(tmp_path / "snap", manifest, STAMP, placed, created, None)
    _swap_files(placed, STAMP)

    assert (state / "secrets.enc").read_text() == "restored secrets"
    assert (state / "skills/desk-check/SKILL.md").read_text() == "restored skill"
    assert not (state / "skills/live-skill").exists()
    aside = state / f".vigil-pre-restore-{STAMP}"
    assert (aside / "skills/live-skill").is_dir()
    assert (aside / "secrets.enc").read_text() == "live secrets"


def test_empty_path_refuses_backed_up_skills(monkeypatch):
    with pytest.raises(BackupError, match="operator skills are off"):
        _target_for("skills", Settings(vigil_skills_path=""), None)
