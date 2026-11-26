"""Counts of hosts CrowdStrike, Defender and Wazuh have not heard from.

One read per vendor, only when that integration is switched on. A failure is
not checked for that vendor and stores no partial count. No hostname, device
id, address or user is kept, logged or put in a reason.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from core.integrations._base.config import missing, resolve
from core.integrations.crowdstrike.client import CrowdStrikeService
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.integrations.microsoft_defender.ingestion import (
    MACHINE_PAGE,
    MachineReadError,
    MicrosoftDefenderIngestion,
)
from core.integrations.wazuh.client import WazuhError, WazuhService
from core.integrations.wazuh.descriptor import WAZUH, WAZUH_REQUIRED
from core.reporting.checks import spec_for
from core.time import utcnow

logger = logging.getLogger(__name__)

_BUDGET_S = 60.0
_DEFENDER_CALLS = 30
ONBOARDED = "onboardingStatus eq 'onboarded'"

_CS = "crowdstrike"
_DEFENDER = "microsoft-defender"
_WAZUH = "wazuh"
_ELASTIC = "elastic-siem"
_OPENSEARCH = "opensearch"

_SETUP = "estate.setup"
_CS_UNHEARD = "estate.crowdstrike.unheard"
_CS_DEGRADED = "estate.crowdstrike.degraded"
_DEF_UNHEARD = "estate.defender.unheard"
_DEF_DEGRADED = "estate.defender.degraded"
_WAZ_UNHEARD = "estate.wazuh.unheard"

# `>` is older than, the same operator Wazuh uses for older_than. A live
# manager confirms the direction; if a known agent lands in the other band,
# these two swap.
_Q_7_30 = "id!=000;status!=never_connected;lastKeepAlive>7d;lastKeepAlive<30d"
_Q_OVER_30 = "id!=000;status!=never_connected;lastKeepAlive>30d"
_Q_NEVER = "id!=000;status=never_connected;dateAdd>7d"
_Q_TOTAL = "id!=000"

_CS_7_30 = "last_seen:<'now-7d'+last_seen:>'now-30d'"
_CS_OVER_30 = "last_seen:<'now-30d'"
_CS_RFM = "reduced_functionality_mode:'yes'"

_NONE_ON = "not checked: none of CrowdStrike, Defender or Wazuh is set up in Vigil"
_UNREAD = (
    "not checked: could not read which of CrowdStrike, Defender or Wazuh is set up"
)
_SOME_AGENTS = (
    "not checked: the Wazuh API user can read only some agents; "
    "allow agent read on all agents"
)
_NO_ALERTS = "Vigil isn't collecting Wazuh alerts"
_DEFENDER_CAP = "not checked: Defender's list is larger than this read will page"
_MACHINES = "https://security.microsoft.com/machines"

_GRANT = {
    "CrowdStrike": "Grant Hosts: Read on the API client in the Falcon console.",
    "Defender": "Grant Machine.Read.All on the app registration.",
    "Wazuh": "Allow agent read on all agents.",
}

_READ_ACTIONS = frozenset({"agent:read", "agent:*", "*:*", "*"})
_EVERY_AGENT = frozenset({"agent:id:*", "agent:*", "*:*", "*:*:*", "*"})

PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not_checked"
NEEDS_A_PERSON = "needs_a_person"


class _Stop(Exception):
    """This vendor's checks are not checked. The reason is ours, not a body."""

    def __init__(self, reason: str, status: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def produce(sources: Optional[list], health_failed: bool) -> list[dict[str, Any]]:
    """The module's checks. A vendor that is switched off is absent."""
    if health_failed or sources is None:
        logger.error("day1 estate check %s", _SETUP)
        return [_stored(_SETUP, NOT_CHECKED, _UNREAD, {})]
    try:
        enabled = _enabled(sources)
    except Exception:  # noqa: BLE001 - the report still completes
        logger.error("day1 estate check %s", _SETUP)
        return [_stored(_SETUP, NOT_CHECKED, _UNREAD, {})]
    if not any(enabled.values()):
        return [_stored(_SETUP, NOT_CHECKED, _NONE_ON, {})]
    stamp = _stamp(utcnow())
    checks: list[dict[str, Any]] = []
    plan = (
        (
            "crowdstrike",
            "CrowdStrike",
            (_CS_UNHEARD, _CS_DEGRADED),
            lambda: _crowdstrike(stamp),
        ),
        (
            "defender",
            "Defender",
            (_DEF_UNHEARD, _DEF_DEGRADED),
            lambda: _defender(stamp),
        ),
        ("wazuh", "Wazuh", (_WAZ_UNHEARD,), lambda: _wazuh(sources, stamp)),
    )
    for key, name, check_ids, run in plan:
        if not enabled[key]:
            continue
        try:
            checks.extend(run())
        except Exception:  # noqa: BLE001 - one vendor does not stop the report
            checks.extend(
                _failed(
                    check_ids, f"not checked: {name} could not be read", None, stamp
                )
            )
    return checks


def _enabled(sources: list) -> dict[str, bool]:
    return {
        "crowdstrike": _on(sources, _CS),
        "defender": _on(sources, _DEFENDER),
        "wazuh": _on(sources, _WAZUH),
    }


def _on(sources: list, source_id: str) -> bool:
    return any(
        row.get("source_id") == source_id and row.get("switched_on") for row in sources
    )


def _stored(
    check_id: str, outcome: str, reason: str, extra: dict[str, Any]
) -> dict[str, Any]:
    """Same evidence rules as the report's other checks: a zero count is kept."""
    spec = spec_for(check_id)
    evidence = {}
    for key, value in extra.items():
        if value is None or value == [] or value == "":
            continue
        evidence[key] = value
    return {
        "id": check_id,
        "module": spec.module,
        "outcome": outcome,
        "reason": reason,
        "evidence": evidence,
    }


def _failed(
    check_ids: tuple[str, ...], reason: str, status: Optional[int], stamp: str
) -> list[dict[str, Any]]:
    for check_id in check_ids:
        if status is None:
            logger.error("day1 estate check %s", check_id)
        else:
            logger.error("day1 estate check %s HTTP %s", check_id, status)
    return [
        _stored(check_id, NOT_CHECKED, reason, {"checked_at": stamp})
        for check_id in check_ids
    ]


def _stamp(now: datetime) -> str:
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return now.isoformat() + "Z"


def _timeout(vendor: str) -> _Stop:
    return _Stop(f"not checked: {vendor} didn't answer within 60s")


def _stop_for(vendor: str, kind: str, status: Optional[int]) -> _Stop:
    grant = _GRANT[vendor]
    if kind == "timeout":
        return _timeout(vendor)
    if kind == "empty":
        return _Stop(f"not checked: {vendor} lists no endpoints", status)
    if status in (401, 403):
        return _Stop(
            f"refused by {vendor} (HTTP {status}); "
            f"check the integration's permissions. {grant}",
            status,
        )
    if status == 429:
        return _Stop(f"rate limited by {vendor}; re-run later", status)
    if kind == "http" and status is not None:
        return _Stop(f"not checked: {vendor} answered HTTP {status}", status)
    return _Stop(f"not checked: {vendor} could not be read", status)


def _past(deadline: float, vendor: str) -> None:
    if time.monotonic() >= deadline:
        raise _timeout(vendor)


def _hosts_page(base_url: Optional[str]) -> str:
    """``api.<cloud>`` is the Falcon console at ``falcon.<cloud>``. No deep link."""
    raw = (base_url or "https://api.crowdstrike.com").strip()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "api.crowdstrike.com").lower()
    if host.startswith("api."):
        host = "falcon." + host[4:]
    return f"https://{host}"


def _crowdstrike(stamp: str) -> list[dict[str, Any]]:
    ids = (_CS_UNHEARD, _CS_DEGRADED)
    try:
        config = resolve(CROWDSTRIKE)
        if missing(config, "client_id", "client_secret"):
            return _failed(
                ids, "not checked: CrowdStrike could not be read", None, stamp
            )
        url = _hosts_page(config.get("base_url"))
        service = CrowdStrikeService(
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            base_url=config.get("base_url") or "https://api.crowdstrike.com",
            timeout=_BUDGET_S,
        )
        deadline = time.monotonic() + _BUDGET_S
        try:
            counts = _cs_counts(service, deadline)
        finally:
            service.session.close()
    except _Stop as stop:
        return _failed(ids, stop.reason, stop.status, stamp)
    except Exception:  # noqa: BLE001 - one vendor does not stop the report
        return _failed(ids, "not checked: CrowdStrike could not be read", None, stamp)
    if counts["listed"] == 0:
        return _empty(("CrowdStrike", ids), stamp)
    heard = counts["days_7_to_30"] + counts["over_30_days"]
    return [
        _stored(
            _CS_UNHEARD,
            NEEDS_A_PERSON if heard else PASS,
            (
                f"{counts['days_7_to_30']} in the last 7–30 days and "
                f"{counts['over_30_days']} over 30 days, of the "
                f"{counts['listed']} endpoints CrowdStrike still lists. "
                "A host with no last seen stays in the total and in neither band. "
                f"See them on the Hosts page at {url}."
            ),
            {
                "listed": counts["listed"],
                "days_7_to_30": counts["days_7_to_30"],
                "over_30_days": counts["over_30_days"],
                "checked_at": stamp,
            },
        ),
        _stored(
            _CS_DEGRADED,
            FAIL if counts["degraded"] else PASS,
            (
                f"{counts['degraded']} in reduced functionality mode, of the "
                f"{counts['listed']} endpoints CrowdStrike still lists. "
                "A blank reduced functionality mode is not counted as degraded. "
                f"See them on the Hosts page at {url}."
            ),
            {
                "listed": counts["listed"],
                "degraded": counts["degraded"],
                "checked_at": stamp,
            },
        ),
    ]


def _cs_counts(service: CrowdStrikeService, deadline: float) -> dict[str, int]:
    listed = _cs_one(service, None, deadline)
    if listed == 0:
        return {"listed": 0}
    return {
        "listed": listed,
        "days_7_to_30": _cs_one(service, _CS_7_30, deadline),
        "over_30_days": _cs_one(service, _CS_OVER_30, deadline),
        "degraded": _cs_one(service, _CS_RFM, deadline),
    }


def _cs_one(
    service: CrowdStrikeService, filter_query: Optional[str], deadline: float
) -> int:
    _past(deadline, "CrowdStrike")
    service.timeout = max(deadline - time.monotonic(), 0.1)
    result = service.device_total(filter_query)
    if time.monotonic() >= deadline and result["kind"] != "ok":
        raise _timeout("CrowdStrike")
    if result["kind"] != "ok" or result["total"] is None:
        raise _stop_for("CrowdStrike", result["kind"], result["status"])
    return int(result["total"])


def _defender(stamp: str) -> list[dict[str, Any]]:
    ids = (_DEF_UNHEARD, _DEF_DEGRADED)
    try:
        counts = _defender_counts()
    except _Stop as stop:
        return _failed(ids, stop.reason, stop.status, stamp)
    except Exception:  # noqa: BLE001 - one vendor does not stop the report
        return _failed(ids, "not checked: Defender could not be read", None, stamp)
    if counts["listed"] == 0:
        return _empty(("Defender", ids), stamp)
    heard = counts["days_7_to_30"] + counts["over_30_days"]
    excluded = (
        "Devices excluded in Defender are still counted, since its API does not "
        "mark them. Defender's device view hides transient devices by default, "
        "so it may show fewer. "
        f"See them at {_MACHINES}."
    )
    return [
        _stored(
            _DEF_UNHEARD,
            NEEDS_A_PERSON if heard else PASS,
            (
                f"{counts['days_7_to_30']} in the last 7–30 days and "
                f"{counts['over_30_days']} over 30 days, of the "
                f"{counts['listed']} endpoints Defender still lists. "
                "Defender's last seen is its last full report, and can be a day "
                f"behind the console. {excluded}"
            ),
            {
                "listed": counts["listed"],
                "days_7_to_30": counts["days_7_to_30"],
                "over_30_days": counts["over_30_days"],
                "checked_at": stamp,
            },
        ),
        _stored(
            _DEF_DEGRADED,
            FAIL if counts["degraded"] else PASS,
            (
                f"{counts['degraded']} with impaired communication or no sensor "
                f"data, of the {counts['listed']} endpoints Defender still lists. "
                "Inactive is counted on the last-seen check, and Unknown is not "
                f"counted as degraded. {excluded}"
            ),
            {
                "listed": counts["listed"],
                "degraded": counts["degraded"],
                "checked_at": stamp,
            },
        ),
    ]


def _defender_counts() -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seven = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    thirty = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    onboarded = ONBOARDED
    degraded = (
        f"{onboarded} and ("
        "healthStatus eq 'ImpairedCommunication' or "
        "healthStatus eq 'NoSensorData' or "
        "healthStatus eq 'NoSensorDataImpairedCommunication')"
    )
    page = MicrosoftDefenderIngestion().count_machine_page
    deadline = time.monotonic() + _BUDGET_S
    calls = [0]
    listed = _def_pages(page, onboarded, deadline, calls, first=True)
    if listed == 0:
        return {"listed": 0}
    return {
        "listed": listed,
        "days_7_to_30": _def_pages(
            page,
            f"{onboarded} and lastSeen lt {seven} and lastSeen ge {thirty}",
            deadline,
            calls,
            first=False,
        ),
        "over_30_days": _def_pages(
            page,
            f"{onboarded} and lastSeen lt {thirty}",
            deadline,
            calls,
            first=False,
        ),
        "degraded": _def_pages(page, degraded, deadline, calls, first=False),
    }


def _def_pages(
    page: Callable[[str, int, float], tuple[int, int]],
    odata_filter: str,
    deadline: float,
    calls: list[int],
    *,
    first: bool,
) -> int:
    """Pages of machines through ``page``, which returns ``(counted, rows)``:
    ``count_machine_page``, or the site module's list read."""
    total = 0
    skip = 0
    while True:
        if calls[0] >= _DEFENDER_CALLS:
            raise _Stop(_DEFENDER_CAP)
        _past(deadline, "Defender")
        timeout = max(deadline - time.monotonic(), 0.1)
        try:
            counted, rows = page(odata_filter, skip, timeout)
        except MachineReadError as exc:
            raise _machine_stop(exc, first=first) from None
        calls[0] += 1
        total += counted
        if rows < MACHINE_PAGE:
            return total
        skip += MACHINE_PAGE


def _machine_stop(exc: MachineReadError, *, first: bool) -> _Stop:
    if exc.kind == "empty" and not first:
        return _Stop("not checked: Defender could not be read", exc.status_code)
    if exc.kind == "cap":
        return _Stop(_DEFENDER_CAP)
    return _stop_for("Defender", exc.kind, exc.status_code)


def _empty(vendor_ids: tuple[str, tuple[str, ...]], stamp: str) -> list[dict]:
    vendor, check_ids = vendor_ids
    reason = f"not checked: {vendor} lists no endpoints"
    return [
        _stored(
            check_id,
            NOT_CHECKED,
            reason,
            {"listed": 0, "checked_at": stamp},
        )
        for check_id in check_ids
    ]


def _wazuh(sources: list, stamp: str) -> list[dict[str, Any]]:
    ids = (_WAZ_UNHEARD,)
    collecting = _on(sources, _ELASTIC) or _on(sources, _OPENSEARCH)
    try:
        counts = asyncio.run(_wazuh_counts())
    except _Stop as stop:
        return _with_alerts(_failed(ids, stop.reason, stop.status, stamp), collecting)
    except Exception:  # noqa: BLE001 - one vendor does not stop the report
        return _with_alerts(
            _failed(ids, "not checked: Wazuh could not be read", None, stamp),
            collecting,
        )
    if counts["listed"] == 0:
        return _with_alerts(_empty(("Wazuh", ids), stamp), collecting)
    heard = counts["days_7_to_30"] + counts["over_30_days"] + counts["never_connected"]
    reason = (
        f"{counts['days_7_to_30']} in the last 7–30 days, "
        f"{counts['over_30_days']} over 30 days, and "
        f"{counts['never_connected']} never connected and registered 7 days "
        f"or more ago, of the {counts['listed']} agents Wazuh still lists. "
        "The manager is not counted. See them on the manager's Agents page."
    )
    return _with_alerts(
        [
            _stored(
                _WAZ_UNHEARD,
                NEEDS_A_PERSON if heard else PASS,
                reason,
                {
                    "listed": counts["listed"],
                    "days_7_to_30": counts["days_7_to_30"],
                    "over_30_days": counts["over_30_days"],
                    "never_connected": counts["never_connected"],
                    "checked_at": stamp,
                },
            )
        ],
        collecting,
    )


def _with_alerts(
    checks: list[dict[str, Any]], collecting: bool
) -> list[dict[str, Any]]:
    if collecting:
        return checks
    for check in checks:
        reason = check["reason"].rstrip()
        if reason.endswith("."):
            reason = reason[:-1]
        check["reason"] = f"{reason}. {_NO_ALERTS}."
    return checks


def _wazuh_service() -> WazuhService:
    """The saved Wazuh credential, with the read budget as its timeout."""
    config = resolve(WAZUH)
    if missing(config, *WAZUH_REQUIRED):
        raise _Stop("not checked: Wazuh could not be read")
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    return WazuhService(
        api_url=config["api_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
        timeout=_BUDGET_S,
    )


async def _wazuh_counts() -> dict[str, int]:
    service = _wazuh_service()
    deadline = time.monotonic() + _BUDGET_S
    try:
        await _full_access(service, deadline)
        listed = await _wazuh_total(service, _Q_TOTAL, deadline)
        if listed == 0:
            return {"listed": 0}
        return {
            "listed": listed,
            "days_7_to_30": await _wazuh_total(service, _Q_7_30, deadline),
            "over_30_days": await _wazuh_total(service, _Q_OVER_30, deadline),
            "never_connected": await _wazuh_total(service, _Q_NEVER, deadline),
        }
    finally:
        await service.close()


async def _full_access(service: WazuhService, deadline: float) -> None:
    """Role 1 may. Otherwise white mode needs an allow on every agent, and
    black mode needs no deny of agent read. A group allow alone does not qualify.
    """
    _ignored, roles = await _wazuh_call(
        service.current_user_access(), deadline, access=True
    )
    if 1 in roles:
        return
    raw = await _wazuh_call(service.my_policies(), deadline, access=True)
    mode = raw.get("rbac_mode") if isinstance(raw, dict) else None
    policies = _policies_of(raw if isinstance(raw, dict) else {})
    if not isinstance(mode, str) or not mode:
        mode = await _wazuh_call(service.rbac_mode(), deadline, access=True)
    mode_name = str(mode).lower()
    if mode_name == "white" and _allows_every(policies):
        return
    if mode_name == "black" and not _denies_read(policies):
        return
    raise _Stop(_SOME_AGENTS)


async def _wazuh_total(service: WazuhService, query: str, deadline: float) -> int:
    return await _wazuh_call(service.agent_total(query), deadline, access=False)


async def _wazuh_call(awaitable, deadline: float, *, access: bool):
    _past(deadline, "Wazuh")
    try:
        return await awaitable
    except WazuhError as exc:
        if time.monotonic() >= deadline:
            raise _timeout("Wazuh") from None
        status = exc.status_code
        if access and status == 403:
            raise _Stop(_SOME_AGENTS, status) from None
        kind = "http" if status is not None else "unreachable"
        if isinstance(exc.__cause__, httpx.TimeoutException):
            kind = "timeout"
        raise _stop_for("Wazuh", kind, status) from None


def _policies_of(data: dict) -> list:
    """Policy rows from either ``affected_items`` or the effective-policy map."""
    items = data.get("affected_items")
    if isinstance(items, list):
        return items
    policies = []
    for action, resources in data.items():
        if action == "rbac_mode" or not isinstance(resources, dict):
            continue
        for resource, effect in resources.items():
            policies.append(
                {
                    "actions": [str(action)],
                    "resources": [str(resource)],
                    "effect": effect,
                }
            )
    return policies


def _policy_bodies(policies: list) -> list[dict]:
    bodies = []
    for item in policies:
        if not isinstance(item, dict):
            continue
        policy = item.get("policy")
        bodies.append(policy if isinstance(policy, dict) else item)
    return bodies


def _names(policy: dict, key: str) -> set[str]:
    raw = policy.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).lower() for item in raw}


def _allows_every(policies: list) -> bool:
    for policy in _policy_bodies(policies):
        effect = str(policy.get("effect") or "allow").lower()
        if effect != "allow":
            continue
        if _names(policy, "actions") & _READ_ACTIONS and (
            _names(policy, "resources") & _EVERY_AGENT
        ):
            return True
    return False


def _denies_read(policies: list) -> bool:
    for policy in _policy_bodies(policies):
        if str(policy.get("effect") or "").lower() != "deny":
            continue
        if _names(policy, "actions") & _READ_ACTIONS:
            return True
    return False
