import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import vigil_path


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
