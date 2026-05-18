"""Service for checking integration compatibility and managing upgrades."""

import re
import sys
import logging
import subprocess
import importlib.metadata
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import json

logger = logging.getLogger(__name__)


# Defense-in-depth guard against any future regression where a non-pinned
# spec slips into the allowlist. The allowlist is server-controlled, but
# rejecting URL/path/flag characters here means even an accidentally bad
# entry can't be turned into RCE via pip install hooks.
_DANGEROUS_SPEC_CHARS = re.compile(r"[\s/\\@;&|]|--|\.\.")
_SAFE_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class IntegrationCompatibilityService:
    """Service for checking integration package compatibility with current Python version."""

    def __init__(self):
        self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self.python_major_minor = f"{sys.version_info.major}.{sys.version_info.minor}"

        # Integration package mappings
        self.integrations = {
            # Threat Intelligence
            "misp": {
                "package": "pymisp",
                "min_version": "2.4.170",
                "display_name": "MISP",
                "category": "Threat Intelligence",
            },
            "opencti": {
                "package": "pycti",
                "min_version": "5.12.0",
                "display_name": "OpenCTI",
                "category": "Threat Intelligence",
            },
            "shodan": {
                "package": "shodan",
                "min_version": "1.30.1",
                "display_name": "Shodan",
                "category": "Threat Intelligence",
            },
            "virustotal": {
                "package": None,  # Uses API directly
                "display_name": "VirusTotal",
                "category": "Threat Intelligence",
            },
            # Incident Management & Ticketing
            "jira": {
                "package": "jira",
                "min_version": "3.5.0",
                "display_name": "Jira",
                "category": "Incident Management",
            },
            "pagerduty": {
                "package": "pdpyras",
                "min_version": "5.1.0",
                "display_name": "PagerDuty",
                "category": "Incident Management",
            },
            "servicenow": {
                "package": "pysnow",
                "min_version": "0.7.17",
                "display_name": "ServiceNow",
                "category": "Incident Management",
                "compatibility_note": "Conflicts with pandas 2.2.0+ (requires old pytz version). Incompatible with Python 3.13.",
            },
            # Communications
            "slack": {
                "package": "slack_sdk",
                "min_version": "3.23.0",
                "display_name": "Slack",
                "category": "Communications",
            },
            "microsoft-teams": {
                "package": "msal",
                "min_version": "1.24.1",
                "display_name": "Microsoft Teams",
                "category": "Communications",
            },
            # EDR/XDR Platforms
            "microsoft-defender": {
                "package": "azure-mgmt-security",
                "min_version": "5.0.0",
                "display_name": "Microsoft Defender",
                "category": "EDR/XDR",
                "dependencies": ["azure-identity"],
            },
            "crowdstrike": {
                "package": None,  # Uses API directly
                "display_name": "CrowdStrike",
                "category": "EDR/XDR",
            },
            "sentinelone": {
                "package": None,  # Uses API directly
                "display_name": "SentinelOne",
                "category": "EDR/XDR",
            },
            "carbon-black": {
                "package": None,  # Uses API directly
                "display_name": "Carbon Black",
                "category": "EDR/XDR",
            },
            # Cloud Security
            "azure-sentinel": {
                "package": "azure-mgmt-sentinel",
                "min_version": "1.0.0",
                "display_name": "Azure Sentinel",
                "category": "Cloud Security",
                "compatibility_note": "Package not available on PyPI. Use azure-monitor-query and azure-mgmt-securityinsight instead.",
            },
            "gcp-security": {
                "package": "google-cloud-security-command-center",
                "min_version": "1.23.0",
                "display_name": "GCP Security Command Center",
                "category": "Cloud Security",
                "compatibility_note": "Not compatible with Python 3.13 (max Python 3.12)",
            },
            # Network Security
            "palo-alto": {
                "package": "pan-os-python",
                "min_version": "1.11.0",
                "display_name": "Palo Alto Networks",
                "category": "Network Security",
            },
            # Vulnerability Management
            "tenable": {
                "package": "tenable-io",
                "min_version": "1.16.0",
                "display_name": "Tenable.io",
                "category": "Vulnerability Management",
                "compatibility_note": "Not compatible with Python 3.13 yet",
            },
            # Data Storage
            "elasticsearch": {
                "package": "elasticsearch",
                "min_version": "8.10.0",
                "display_name": "Elasticsearch",
                "category": "Data Storage",
            },
            "postgresql": {
                "package": "psycopg2-binary",
                "min_version": "2.9.9",
                "display_name": "PostgreSQL",
                "category": "Data Storage",
            },
            # Core
            "claude-agent-sdk": {
                "package": "claude-agent-sdk",
                "min_version": "0.1.0",
                "display_name": "Claude Agent SDK",
                "category": "Core",
                "python_min_version": "3.10",
            },
        }

    def check_package_installed(self, package_name: str) -> Tuple[bool, Optional[str]]:
        """
        Check if a package is installed and return its version.

        Returns:
            (is_installed, version)
        """
        if not package_name:
            return (True, None)  # API-only integration

        try:
            version = importlib.metadata.version(package_name)
            return (True, version)
        except importlib.metadata.PackageNotFoundError:
            return (False, None)

    def check_python_compatibility(
        self, integration_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if current Python version is compatible with the integration.

        Returns:
            (is_compatible, reason)
        """
        integration = self.integrations.get(integration_id, {})

        # Check Python minimum version requirement
        if "python_min_version" in integration:
            min_version = integration["python_min_version"]
            if sys.version_info < tuple(map(int, min_version.split("."))):
                return (
                    False,
                    f"Requires Python {min_version}+, you have {self.python_version}",
                )

        # Check specific compatibility notes
        if "compatibility_note" in integration:
            note = integration["compatibility_note"]

            # Check if note mentions Python version incompatibility
            if "Python 3.13" in note and sys.version_info >= (3, 13):
                return (False, note)
            elif "Python 3.12" in note and sys.version_info >= (3, 13):
                return (False, note)

        return (True, None)

    def get_integration_status(self, integration_id: str) -> Dict:
        """
        Get complete status for an integration.

        Returns:
            Dictionary with status information
        """
        integration = self.integrations.get(integration_id, {})

        if not integration:
            return {
                "integration_id": integration_id,
                "status": "unknown",
                "message": "Integration not found",
            }

        package_name = integration.get("package")
        display_name = integration.get("display_name", integration_id)

        # Check Python compatibility first
        python_compatible, python_reason = self.check_python_compatibility(
            integration_id
        )

        if not python_compatible:
            return {
                "integration_id": integration_id,
                "display_name": display_name,
                "status": "incompatible",
                "message": python_reason,
                "installed": False,
                "can_install": False,
                "compatibility_note": integration.get("compatibility_note"),
            }

        # Check if package is installed
        if package_name:
            is_installed, current_version = self.check_package_installed(package_name)

            if is_installed:
                return {
                    "integration_id": integration_id,
                    "display_name": display_name,
                    "status": "installed",
                    "message": f"Installed (v{current_version})",
                    "installed": True,
                    "version": current_version,
                    "can_install": False,
                    "can_upgrade": True,
                    "package": package_name,
                }
            else:
                return {
                    "integration_id": integration_id,
                    "display_name": display_name,
                    "status": "not_installed",
                    "message": "Not installed",
                    "installed": False,
                    "can_install": True,
                    "package": package_name,
                    "min_version": integration.get("min_version"),
                }
        else:
            # API-only integration
            return {
                "integration_id": integration_id,
                "display_name": display_name,
                "status": "available",
                "message": "API-based (no package required)",
                "installed": True,
                "can_install": False,
            }

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get status for all integrations."""
        statuses = {}
        for integration_id in self.integrations.keys():
            statuses[integration_id] = self.get_integration_status(integration_id)
        return statuses

    def get_allowed_integration_ids(self) -> List[str]:
        """Return integration IDs that have a known pip-installable package.

        Drives the allowlist used by ``install_known_integration``; the
        UI can also call this to render only the integrations that the
        backend is willing to install.
        """
        return [
            integration_id
            for integration_id, info in self.integrations.items()
            if info.get("package") and info.get("min_version")
        ]

    def install_known_integration(self, integration_id: str) -> Tuple[bool, str]:
        """Install the pinned package for a known integration ID.

        The package name and minimum version come from the server-side
        ``self.integrations`` map — the caller never supplies a package
        spec directly. This closes the unauthenticated-pip-install RCE
        from the 2026-05 disclosure: even an authenticated admin can't
        ask pip to fetch arbitrary URLs/paths/VCS refs.
        """
        info = self.integrations.get(integration_id)
        if not info or not info.get("package"):
            return (False, f"Unknown or non-installable integration: {integration_id}")

        package_name = info["package"]
        min_version = info.get("min_version")

        if not _SAFE_PACKAGE_NAME_RE.match(package_name):
            return (False, f"Invalid package name in allowlist: {package_name}")
        if min_version and _DANGEROUS_SPEC_CHARS.search(min_version):
            return (False, f"Invalid version in allowlist: {min_version}")

        package_spec = f"{package_name}>={min_version}" if min_version else package_name
        if _DANGEROUS_SPEC_CHARS.search(package_spec):
            return (False, f"Refusing to install dangerous spec: {package_spec}")

        return self._run_pip_install(package_spec, label=integration_id)

    def _run_pip_install(self, package_spec: str, *, label: str) -> Tuple[bool, str]:
        """Execute ``pip install --upgrade`` with the given spec.

        Private helper — never accepts raw user input. Uses list-form
        subprocess args so there's no shell, and pins the
        executable to ``sys.executable`` so we install into the
        current virtualenv rather than whatever ``pip`` is on PATH.
        """
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--disable-pip-version-check",
                    package_spec,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                logger.info("Installed integration package: %s", label)
                return (True, f"Successfully installed {label}")
            stderr_tail = (result.stderr or "")[-1000:]
            logger.warning("Installation failed for %s: %s", label, stderr_tail)
            return (False, f"Installation failed: {stderr_tail}")

        except subprocess.TimeoutExpired:
            return (False, "Installation timed out")
        except Exception as e:
            return (False, f"Installation error: {str(e)}")

    def upgrade_known_integration(self, integration_id: str) -> Tuple[bool, str]:
        """Upgrade the pinned package for a known integration."""
        return self.install_known_integration(integration_id)

    def uninstall_known_integration(self, integration_id: str) -> Tuple[bool, str]:
        """Uninstall the package backing a known integration."""
        info = self.integrations.get(integration_id)
        if not info or not info.get("package"):
            return (False, f"Unknown or non-installable integration: {integration_id}")

        package_name = info["package"]
        if not _SAFE_PACKAGE_NAME_RE.match(package_name):
            return (False, f"Invalid package name in allowlist: {package_name}")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "uninstall",
                    "-y",
                    package_name,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                logger.info("Uninstalled integration package: %s", integration_id)
                return (True, f"Successfully uninstalled {integration_id}")
            stderr_tail = (result.stderr or "")[-1000:]
            return (False, f"Uninstallation failed: {stderr_tail}")

        except Exception as e:
            return (False, f"Uninstallation error: {str(e)}")

    def get_system_info(self) -> Dict:
        """Get system information."""
        return {
            "python_version": self.python_version,
            "python_major_minor": self.python_major_minor,
            "python_implementation": sys.implementation.name,
            "platform": sys.platform,
            "pip_version": self._get_pip_version(),
        }

    def _get_pip_version(self) -> Optional[str]:
        """Get pip version."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Parse "pip X.Y.Z from ..."
                parts = result.stdout.split()
                if len(parts) >= 2:
                    return parts[1]
        except Exception:
            pass
        return None


# Singleton instance
_compatibility_service = None


def get_compatibility_service() -> IntegrationCompatibilityService:
    """Get singleton compatibility service instance."""
    global _compatibility_service
    if _compatibility_service is None:
        _compatibility_service = IntegrationCompatibilityService()
    return _compatibility_service
