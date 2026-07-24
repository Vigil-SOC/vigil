import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / '.deeptempo'
INTEGRATIONS_FILE = CONFIG_DIR / 'integrations_config.json'
GENERAL_CONFIG_FILE = CONFIG_DIR / 'general_config.json'

REQUEST_TIMEOUT = 30
STREAM_TIMEOUT = 120


def get_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def is_demo_mode() -> bool:
    """Check if demo mode is enabled via environment or config."""
    # Check environment variable first
    env_demo = os.getenv('DEMO_MODE', '').lower()
    if env_demo in ('true', '1', 'yes'):
        return True
    if env_demo in ('false', '0', 'no'):
        return False
    # Fall back to general config
    return get_general_config('demo_mode', False)


def _load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Config load error {path}: {e}")
        return {}


def get_integration_config(integration_id: str) -> dict[str, Any]:
    data = _load_json_config(INTEGRATIONS_FILE)
    if integration_id not in data.get('enabled_integrations', []):
        return {}
    return data.get('integrations', {}).get(integration_id, {})


def is_integration_enabled(integration_id: str) -> bool:
    data = _load_json_config(INTEGRATIONS_FILE)
    return integration_id in data.get('enabled_integrations', [])


def get_enabled_integrations() -> list[str]:
    data = _load_json_config(INTEGRATIONS_FILE)
    return data.get('enabled_integrations', [])


def get_general_config(key: str, default: Any = None) -> Any:
    data = _load_json_config(GENERAL_CONFIG_FILE)
    return data.get(key, default)
