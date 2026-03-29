"""
Pytest configuration for scripts directory.

Provides fixtures needed by script-based tests and skips
integration tests when external services are not configured.
"""

import os
import pytest


@pytest.fixture
def config():
    """
    Provide Splunk connection config from environment variables.

    Skips the test automatically when SPLUNK_HOST is not set,
    preventing collection/setup errors in environments without
    a live Splunk instance.
    """
    splunk_host = os.environ.get("SPLUNK_HOST")
    if not splunk_host:
        pytest.skip("SPLUNK_HOST environment variable not set — skipping Splunk integration test")

    return {
        "url": f"https://{splunk_host}:{os.environ.get('SPLUNK_PORT', '8089')}",
        "username": os.environ.get("SPLUNK_USERNAME", ""),
        "password": os.environ.get("SPLUNK_PASSWORD", ""),
        "verify_ssl": os.environ.get("SPLUNK_VERIFY_SSL", "false").lower() == "true",
    }
