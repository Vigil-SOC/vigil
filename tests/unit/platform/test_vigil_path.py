import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# chmod cannot deny root, so the unwritable cases prove nothing there.
needs_unprivileged = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="requires a non-root user"
)

from core.config import state_dir_status, vigil_path


@pytest.fixture
def home(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        yield tmp_path


@pytest.mark.unit
def test_read_prefers_vigil_dir(home):
    (home / ".vigil").mkdir()
    (home / ".deeptempo").mkdir()
    (home / ".vigil" / "a.json").write_text("{}")
    (home / ".deeptempo" / "a.json").write_text("{}")
    assert vigil_path("a.json") == home / ".vigil" / "a.json"


@pytest.mark.unit
def test_read_falls_back_to_legacy_dir(home):
    # The compatibility guarantee: an install whose data only exists under
    # ~/.deeptempo keeps reading it, with no migration step.
    (home / ".deeptempo").mkdir()
    legacy = home / ".deeptempo" / "integrations_config.json"
    legacy.write_text(json.dumps({"enabled_integrations": ["vstrike"]}))
    assert vigil_path("integrations_config.json") == legacy


@pytest.mark.unit
def test_read_of_missing_file_points_at_vigil_dir(home):
    assert vigil_path("nope.json") == home / ".vigil" / "nope.json"


@pytest.mark.unit
def test_write_always_targets_vigil_dir_even_when_legacy_exists(home):
    (home / ".deeptempo").mkdir()
    (home / ".deeptempo" / "theme_config.json").write_text("{}")
    target = vigil_path("theme_config.json", write=True)
    assert target == home / ".vigil" / "theme_config.json"
    assert target.parent.is_dir()


@pytest.mark.unit
def test_write_with_no_parts_creates_the_directory_itself(home):
    assert vigil_path(write=True) == home / ".vigil"
    assert (home / ".vigil").is_dir()


@pytest.mark.unit
def test_vigil_dir_overrides_home(tmp_path, monkeypatch):
    custom = tmp_path / "state"
    monkeypatch.setenv("VIGIL_DIR", str(custom))
    assert vigil_path("a.json") == custom / "a.json"
    assert vigil_path("a.json", write=True) == custom / "a.json"
    assert custom.is_dir()


@pytest.mark.unit
def test_vigil_dir_has_no_legacy_fallback(home, tmp_path, monkeypatch):
    # An explicit override means exactly that directory.
    (home / ".deeptempo").mkdir()
    (home / ".deeptempo" / "a.json").write_text("{}")
    custom = tmp_path / "state"
    monkeypatch.setenv("VIGIL_DIR", str(custom))
    assert vigil_path("a.json") == custom / "a.json"


@pytest.mark.unit
@needs_unprivileged
def test_write_failure_raises_rather_than_relocating(home):
    # A silent /tmp relocation applied to writes only, so the save looked fine
    # and the value was gone. Callers that want to degrade catch this themselves.
    home.chmod(0o500)
    try:
        with pytest.raises(OSError):
            vigil_path("a.json", write=True)
    finally:
        home.chmod(0o700)


@pytest.mark.unit
def test_state_dir_status_reports_path_and_writability(home):
    status = state_dir_status()
    assert status == {"path": str(home / ".vigil"), "exists": False, "writable": True}
    (home / ".vigil").mkdir()
    assert state_dir_status()["exists"] is True


@pytest.mark.unit
@needs_unprivileged
def test_state_dir_status_reports_unwritable_without_raising(home):
    home.chmod(0o500)
    try:
        status = state_dir_status()
        assert status["writable"] is False
        assert status["path"] == str(home / ".vigil")
    finally:
        home.chmod(0o700)


@pytest.mark.unit
def test_bare_directory_never_resolves_to_legacy(home):
    # The secrets backend asks for the directory itself; answering ~/.deeptempo
    # would send every credential written after it there.
    (home / ".deeptempo").mkdir()
    assert vigil_path() == home / ".vigil"


@pytest.mark.unit
def test_bare_directory_is_not_created_on_read(home):
    assert not (home / ".vigil").exists()
    vigil_path()
    state_dir_status()
    assert not (home / ".vigil").exists()


@pytest.mark.unit
def test_root_home_falls_back_to_safe_directory(monkeypatch):
    monkeypatch.delenv("VIGIL_DIR", raising=False)
    with patch.object(Path, "home", return_value=Path("/")):
        target = vigil_path("test.json")
        assert target != Path("/.vigil/test.json")
        assert target.name == "test.json"
        assert target.parent.name == ".vigil"
        assert str(target).startswith("/home/vigil") or str(target).startswith("/tmp")


@pytest.mark.unit
def test_root_home_safe_write(monkeypatch, tmp_path):
    monkeypatch.delenv("VIGIL_DIR", raising=False)
    with patch.object(Path, "home", return_value=Path("/")):
        with patch("core.config._safe_home", return_value=tmp_path):
            target = vigil_path("test.json", write=True)
            assert target == tmp_path / ".vigil" / "test.json"
            assert (tmp_path / ".vigil").is_dir()


@pytest.mark.unit
def test_unwritable_home_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("VIGIL_DIR", raising=False)
    unwritable = Path("/nonexistent_root_dir/sub")
    with patch.object(Path, "home", return_value=unwritable):
        target = vigil_path("test.json")
        assert str(target).startswith("/tmp")


