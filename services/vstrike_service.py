"""Outbound REST client for the VStrike (CloudCurrent) fusion layer.

VStrike pushes enriched findings to Vigil, but we also query it for asset
topology, adjacent-asset lookup, and blast-radius computation during
investigations. This service is consumed by `backend/api/vstrike.py` (proxy
endpoints) and `tools/vstrike.py` (MCP server).

Auth: bearer token in the `Authorization` header. Read from env var
`VSTRIKE_API_KEY` or from the per-integration config
(`core.config.get_integration_config("vstrike")`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class VStrikeService:
    """Thin REST client for the VStrike API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        return self.session.get(url, timeout=self.timeout, **kwargs)

    def test_connection(self) -> Tuple[bool, str]:
        """Ping the VStrike health endpoint.

        Returns a (success, message) tuple.
        """
        try:
            response = self._get("/api/v1/health")
            if response.status_code == 200:
                return True, "Connection successful"
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
        except requests.exceptions.RequestException as e:
            return False, f"Connection error: {e}"

    def get_asset_topology(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Return full topology info for an asset (neighbors, segment, site)."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}")
            if response.status_code == 200:
                return response.json()
            logger.warning(
                "VStrike get_asset_topology(%s) returned HTTP %s",
                asset_id,
                response.status_code,
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike get_asset_topology(%s) failed: %s", asset_id, e)
            return None

    def list_adjacent(self, asset_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return adjacent assets (one hop) for an asset."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}/adjacent")
            if response.status_code == 200:
                return response.json().get("adjacent", [])
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike list_adjacent(%s) failed: %s", asset_id, e)
            return None

    def get_blast_radius(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Return blast-radius info (count + sample assets) for an asset."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}/blast-radius")
            if response.status_code == 200:
                return response.json()
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike get_blast_radius(%s) failed: %s", asset_id, e)
            return None

    def find_findings_by_segment(
        self, segment: str, limit: int = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """Return VStrike-enriched findings for a network segment."""
        try:
            response = self._get(
                "/api/v1/findings",
                params={"segment": segment, "limit": limit},
            )
            if response.status_code == 200:
                return response.json().get("findings", [])
            return None
        except requests.exceptions.RequestException as e:
            logger.error(
                "VStrike find_findings_by_segment(%s) failed: %s", segment, e
            )
            return None


def _config_value(key: str, config: Optional[Dict[str, Any]]) -> Optional[str]:
    if config is None:
        return None
    return config.get(key)


def get_vstrike_service() -> Optional[VStrikeService]:
    """Construct a VStrikeService from env or integration config, or None.

    Precedence (first hit wins):
      1. `VSTRIKE_BASE_URL` + `VSTRIKE_API_KEY` env vars
      2. `get_integration_config("vstrike")` → `{url, api_key, verify_ssl}`
    """
    base_url = os.environ.get("VSTRIKE_BASE_URL")
    api_key = os.environ.get("VSTRIKE_API_KEY")
    verify_ssl_env = os.environ.get("VSTRIKE_VERIFY_SSL", "true").lower() != "false"

    config: Optional[Dict[str, Any]] = None
    if not (base_url and api_key):
        try:
            from core.config import get_integration_config

            config = get_integration_config("vstrike")
        except Exception as e:
            logger.debug("VStrike integration config not loaded: %s", e)
            config = None

    base_url = base_url or _config_value("url", config)
    api_key = api_key or _config_value("api_key", config)

    if not base_url or not api_key:
        return None

    verify_ssl = verify_ssl_env
    if config is not None and "verify_ssl" in config:
        verify_ssl = bool(config.get("verify_ssl", True))

    return VStrikeService(base_url=base_url, api_key=api_key, verify_ssl=verify_ssl)
