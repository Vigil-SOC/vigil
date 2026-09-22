"""The secrets backend honours VIGIL_DISABLE_DOTENV too (GH #974 follow-up).

The vendor tool servers were the loud half of this leak: their import-time
``load_dotenv()`` wrote a developer's whole ``.env`` into ``os.environ``, which
pydantic reads whatever ``Settings.env_file`` says, so every later
``get_settings()`` answered from that file. ``test_tool_dotenv_disabled.py``
pins that half shut.

``DotEnvBackend`` is the quiet half. It reads the *state directory's* ``.env``
into its own cache and never touches ``os.environ``, so it moves nothing
``get_settings()`` can see -- it just hands a test that asks for a credential
the operator's real one. Same leak, different coat.

Only the file it reaches for on its own is skipped. A caller that names a file
has said which one it means, and the dotenv-to-encrypted migration builds a
backend over a named file on purpose; guarding that would break the migration
rather than close anything.
"""

from __future__ import annotations

import pytest

from core.secrets_manager import DotEnvBackend

pytestmark = pytest.mark.unit

SECRET = "SPLUNK_PASSWORD"


@pytest.fixture
def state_dir_dotenv(tmp_path, monkeypatch):
    """Point VIGIL_DIR at a tmp state directory holding a credential."""
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    (tmp_path / ".env").write_text(f"{SECRET}=the-operators-real-one\n")
    return tmp_path


def test_default_file_is_not_read_under_the_suite(state_dir_dotenv, monkeypatch):
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")

    assert DotEnvBackend().get(SECRET) is None, (
        "A test asking for a credential was handed the value from the "
        "operator's .env. Every path that loads one has to honour "
        "VIGIL_DISABLE_DOTENV, not only the ones that write into os.environ."
    )


def test_default_file_is_still_read_outside_a_test_run(state_dir_dotenv, monkeypatch):
    """The other direction: this file is where an operator's secrets live."""
    monkeypatch.delenv("VIGIL_DISABLE_DOTENV", raising=False)

    assert DotEnvBackend().get(SECRET) == "the-operators-real-one"


def test_a_file_the_caller_named_is_read_either_way(tmp_path, monkeypatch):
    """Naming it is the asking -- the migration builds one over a named file."""
    named = tmp_path / "named.env"
    named.write_text(f"{SECRET}=one-the-caller-asked-for\n")
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")

    assert DotEnvBackend(named).get(SECRET) == "one-the-caller-asked-for"
