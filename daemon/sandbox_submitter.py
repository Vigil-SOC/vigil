"""Sandbox auto-submission with safety gating.

Entry point for the daemon's opt-in detonation pipeline. Given a file hash
extracted from a finding, this module:

1. Respects the master ``SANDBOX_AUTO_SUBMIT`` switch (default: disabled).
2. Enforces a file-type allowlist and a max-size cap.
3. Checks each enabled sandbox's hash cache first — only submits when unknown.
4. Submits in parallel to all enabled sandboxes, returning their task IDs
   for later polling.

Only hash-based submissions are supported here. File bytes (required for the
initial detonation) must already live in an upstream system; the current
pipeline operates on hashes observed in events. The submit step therefore
tells the sandbox "detonate this hash" only when the sandbox's own cache has
already seen the binary. This is a conservative default — safer than
exfiltrating arbitrary binaries from the Vigil process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_TYPES = "exe,dll,doc,docx,xls,xlsx,pdf,js,vbs,ps1,bat,msi"
_MD5_LEN = 32
_SHA1_LEN = 40
_SHA256_LEN = 64


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class SandboxSettings:
    auto_submit: bool
    max_file_size_mb: int
    allowed_types: List[str]
    timeout_seconds: int
    joe_enabled: bool
    cape_enabled: bool
    hybrid_enabled: bool
    anyrun_enabled: bool

    @classmethod
    def from_env(cls) -> "SandboxSettings":
        allowed = os.getenv("SANDBOX_ALLOWED_FILE_TYPES", _DEFAULT_ALLOWED_TYPES)
        return cls(
            auto_submit=_env_bool("SANDBOX_AUTO_SUBMIT", False),
            max_file_size_mb=_env_int("SANDBOX_MAX_FILE_SIZE_MB", 100),
            allowed_types=[t.strip().lower() for t in allowed.split(",") if t.strip()],
            timeout_seconds=_env_int("SANDBOX_ANALYSIS_TIMEOUT", 300),
            joe_enabled=_env_bool("JOE_SANDBOX_ENABLED", False),
            cape_enabled=_env_bool("CAPE_SANDBOX_ENABLED", False),
            hybrid_enabled=_env_bool("HYBRID_ANALYSIS_ENABLED", False),
            anyrun_enabled=_env_bool("ANYRUN_ENABLED", False),
        )


class SandboxSubmitter:
    """Encapsulates hash-based sandbox submission with safety gating.

    Designed to be instantiated once per daemon process and reused across
    many findings. All HTTP calls are routed through ``asyncio.to_thread``
    so the caller stays async.
    """

    def __init__(self, settings: Optional[SandboxSettings] = None):
        self.settings = settings or SandboxSettings.from_env()

    # ---------- public API ----------

    def enabled(self) -> bool:
        """Return True if auto-submission is globally on and at least one
        sandbox is enabled."""
        return self.settings.auto_submit and (
            self.settings.joe_enabled
            or self.settings.cape_enabled
            or self.settings.hybrid_enabled
            or self.settings.anyrun_enabled
        )

    def is_hash_safe_to_submit(
        self, hash_val: str, file_hint: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Apply safety rules before any network call.

        ``file_hint`` may include ``file_name``, ``file_size`` pulled from
        the finding's entity_context when available; when missing, the
        hash-only path is allowed (we cannot verify type/size upstream).
        """
        if not self._looks_like_hash(hash_val):
            return False

        hint = file_hint or {}
        size = hint.get("file_size")
        if size is not None:
            try:
                if int(size) > self.settings.max_file_size_mb * 1024 * 1024:
                    logger.info("Skipping sandbox submission: size exceeds cap")
                    return False
            except (TypeError, ValueError):
                pass

        fname = hint.get("file_name")
        if fname:
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext and ext not in self.settings.allowed_types:
                logger.info(
                    "Skipping sandbox submission: extension %s not allowlisted", ext
                )
                return False

        return True

    async def submit_hash(
        self,
        hash_val: str,
        file_hint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Submit a hash to all enabled sandboxes in parallel.

        Returns a dict like::

            {
                "cape": {"status": "cached", "task_id": "123"},
                "hybrid_analysis": {"status": "submitted", "task_id": "..."},
                "joe_sandbox": {"status": "skipped", "reason": "disabled"},
            }
        """
        if not self.enabled():
            return {"status": "disabled"}

        if not self.is_hash_safe_to_submit(hash_val, file_hint):
            return {"status": "rejected", "reason": "safety_gate"}

        coros = []
        names: List[str] = []

        if self.settings.cape_enabled:
            names.append("cape")
            coros.append(self._submit_cape(hash_val))
        if self.settings.hybrid_enabled:
            names.append("hybrid_analysis")
            coros.append(self._submit_hybrid(hash_val))
        if self.settings.anyrun_enabled:
            names.append("anyrun")
            coros.append(self._submit_anyrun(hash_val))
        if self.settings.joe_enabled:
            names.append("joe_sandbox")
            coros.append(self._submit_joe(hash_val))

        results = await asyncio.gather(*coros, return_exceptions=True)

        now_iso = datetime.utcnow().isoformat()
        out: Dict[str, Any] = {}
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                out[name] = {
                    "status": "error",
                    "error": str(res),
                    "submitted_at": now_iso,
                }
            else:
                if isinstance(res, dict):
                    res.setdefault("submitted_at", now_iso)
                out[name] = res
        return out

    # ---------- per-sandbox handlers ----------

    async def _submit_cape(self, hash_val: str) -> Dict[str, Any]:
        url = os.getenv("CAPE_SANDBOX_URL", "").rstrip("/")
        api_key = os.getenv("CAPE_SANDBOX_API_KEY", "")
        if not url:
            return {"status": "skipped", "reason": "no_url"}
        headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        hash_type = self._hash_type(hash_val)
        if not hash_type:
            return {"status": "skipped", "reason": "unknown_hash_format"}
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{url}/apiv2/tasks/search/{hash_type}/{hash_val}/",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                tasks = data.get("data") if isinstance(data, dict) else data
                if tasks:
                    task_id = tasks[0].get("id") if isinstance(tasks[0], dict) else None
                    return {
                        "status": "cached",
                        "task_id": str(task_id) if task_id else None,
                    }
        except Exception as e:
            logger.debug("CAPE hash search failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {
            "status": "unknown",
            "note": "Hash not in CAPE cache; binary upload required for detonation",
        }

    async def _submit_hybrid(self, hash_val: str) -> Dict[str, Any]:
        from core.config import get_integration_config

        cfg = get_integration_config("hybrid_analysis") or {}
        api_key = cfg.get("api_key") or os.getenv("HYBRID_ANALYSIS_API_KEY", "")
        if not api_key:
            return {"status": "skipped", "reason": "no_api_key"}
        try:
            resp = await asyncio.to_thread(
                requests.post,
                "https://www.hybrid-analysis.com/api/v2/search/hash",
                headers={"api-key": api_key, "User-Agent": "Falcon Sandbox"},
                data={"hash": hash_val},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    jid = data[0].get("job_id") if isinstance(data[0], dict) else None
                    return {"status": "cached", "task_id": jid}
        except Exception as e:
            logger.debug("Hybrid Analysis hash search failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {"status": "unknown"}

    async def _submit_anyrun(self, hash_val: str) -> Dict[str, Any]:
        from core.config import get_integration_config

        cfg = get_integration_config("anyrun") or {}
        api_key = cfg.get("api_key") or os.getenv("ANYRUN_API_KEY", "")
        if not api_key:
            return {"status": "skipped", "reason": "no_api_key"}
        try:
            resp = await asyncio.to_thread(
                requests.get,
                "https://api.any.run/v1/tasks",
                headers={"Authorization": f"API-Key {api_key}"},
                params={"hash": hash_val},
                timeout=15,
            )
            if resp.status_code == 200:
                tasks = resp.json().get("data", {}).get("tasks", [])
                if tasks:
                    tid = tasks[0].get("uuid") or tasks[0].get("id")
                    return {"status": "cached", "task_id": tid}
        except Exception as e:
            logger.debug("Any.Run hash search failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {"status": "unknown"}

    async def _submit_joe(self, hash_val: str) -> Dict[str, Any]:
        api_key = os.getenv("JOE_SANDBOX_API_KEY", "") or os.getenv("JBXAPIKEY", "")
        base = os.getenv(
            "JOE_SANDBOX_URL", "https://jbxcloud.joesecurity.org/api"
        ).rstrip("/")
        if not api_key:
            return {"status": "skipped", "reason": "no_api_key"}
        try:
            resp = await asyncio.to_thread(
                requests.post,
                f"{base}/v2/analysis/search",
                data={"apikey": api_key, "q": hash_val},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    webid = data[0].get("webid")
                    return {"status": "cached", "task_id": webid}
        except Exception as e:
            logger.debug("Joe Sandbox hash search failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {"status": "unknown"}

    # ---------- helpers ----------

    @staticmethod
    def _looks_like_hash(s: str) -> bool:
        if not s or not isinstance(s, str):
            return False
        s = s.strip().lower()
        if len(s) not in (_MD5_LEN, _SHA1_LEN, _SHA256_LEN):
            return False
        return all(c in "0123456789abcdef" for c in s)

    @staticmethod
    def _hash_type(s: str) -> Optional[str]:
        length = len(s.strip())
        if length == _MD5_LEN:
            return "md5"
        if length == _SHA1_LEN:
            return "sha1"
        if length == _SHA256_LEN:
            return "sha256"
        return None
