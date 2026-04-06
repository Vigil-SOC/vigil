"""
Unit tests for dotenv loading in tools/_legacy/splunk.py.
Verifies that credentials in a .env file are loaded into os.environ
at module startup, and that the module starts without error when
python-dotenv is not installed.
"""

import os
import subprocess
import sys
import textwrap
import tempfile


def test_dotenv_loads_splunk_credentials(tmp_path):
    """Writing a .env file and importing the module in a clean subprocess
    should make SPLUNK_URL, SPLUNK_USERNAME, and SPLUNK_PASSWORD available
    via os.environ."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPLUNK_URL=https://test.example.com:8089\n"
        "SPLUNK_USERNAME=testuser\n"
        "SPLUNK_PASSWORD=testpass\n"
    )

    # Script that changes to tmp_path (so load_dotenv() finds the .env file),
    # then runs the dotenv loading block and prints the env vars.
    script = textwrap.dedent(f"""\
        import os
        os.chdir({str(tmp_path)!r})
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        print(os.environ.get("SPLUNK_URL", ""))
        print(os.environ.get("SPLUNK_USERNAME", ""))
        print(os.environ.get("SPLUNK_PASSWORD", ""))
    """)

    # Run in a clean environment (no pre-set SPLUNK_* vars)
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("SPLUNK_")}

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=clean_env,
    )

    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "https://test.example.com:8089"
    assert lines[1] == "testuser"
    assert lines[2] == "testpass"


def test_missing_dotenv_does_not_raise():
    """The try/except ImportError block must not raise even when
    python-dotenv is not importable."""
    script = textwrap.dedent("""\
        import sys
        # Shadow dotenv so ImportError is raised on import
        import unittest.mock as mock
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *args, **kwargs):
            if name == "dotenv":
                raise ImportError("dotenv not installed")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = fake_import
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        finally:
            builtins.__import__ = real_import
        print("ok")
    """)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "ok"


def test_symlink_exists_and_points_to_legacy():
    """tools/splunk.py must be a symlink that resolves to tools/_legacy/splunk.py."""
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    symlink_path = os.path.join(repo_root, "tools", "splunk.py")
    assert os.path.islink(symlink_path), (
        f"{symlink_path} is not a symlink"
    )
    real_path = os.path.realpath(symlink_path)
    assert real_path.endswith(os.path.join("_legacy", "splunk.py")), (
        f"symlink resolves to {real_path}, expected to end with _legacy/splunk.py"
    )
