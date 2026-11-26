"""Does a proposed action's target fit its action type (#4963).

One table, keyed on the kinds a run may propose (run_bridge_router's
PROPOSAL_TITLES). The rest of ActionType is not a proposal surface and keeps the
service's own checks. Each entry returns None when the target fits or a reason
code when it does not; the caller files the stripped target unchanged.
"""

from __future__ import annotations

import ipaddress
import re
import string
from typing import Any, Callable, Dict, Optional, Tuple

# One sentence per code, so the case trail and the MCP error word a code alike.
REASON_SENTENCES = {
    "action_type_unknown": "the action is not one a run may propose",
    "target_missing": "no target was given",
    "reason_missing": "no reason was given",
    "confidence_missing": "no confidence was given",
    "confidence_zero": "the confidence is zero, which is no claim",
    "confidence_out_of_range": "the confidence is not a number between 0 and 1",
    "target_is_placeholder": "the target is a placeholder, not a value",
    "target_too_long": "the target is longer than the action allows",
    "target_not_a_host": "the target is not a host name, address or device id",
    "target_not_an_address": "the target is not an address or network",
    "target_not_a_domain": (
        "the target is not a domain name (a machine name belongs in isolate_host,"
        " an address in block_ip)"
    ),
    "target_not_a_path_or_hash": "the target is not an absolute path or a file hash",
    "target_not_a_user": "the target is not an account",
}

# How each kind reads in "The run asked to ..." on the case.
ACTION_PHRASES = {
    "isolate_host": "isolate a host",
    "block_ip": "block an address",
    "block_domain": "block a domain",
    "quarantine_file": "quarantine a file",
    "disable_user": "disable an account",
    "revoke_session": "end an account's sessions",
    "custom": "take an action",
}

_NON_ANSWERS = {
    "unknown", "unk", "tbd", "tba", "n/a", "na", "none", "null", "nil",
    "host", "user", "file", "domain", "ip", "address",
    "the host", "the user", "the file", "the domain", "the ip", "the address",
}  # fmt: skip
_TEMPLATE_MARKERS = ("<", ">", "{{", "}}", "${", "%s")
_EDGE = string.punctuation + string.whitespace

_HOST = re.compile(r"[A-Za-z0-9_-]{1,63}(\.[A-Za-z0-9_-]{1,63})*")
_DEVICE = re.compile(r"[A-Za-z0-9.:_-]{1,128}")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TLD = re.compile(r"[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59}")
_HASH = re.compile(r"[0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")
_SID = re.compile(r"S-1-\d+(-\d+)+", re.IGNORECASE)
_UPN = re.compile(r"^[^@\s/\\]+@[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+")
_DOWN_LEVEL = re.compile(r"^[^\\\s/@]+\\[^\\\s/@]+")
_ACCOUNT = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The run before the first alphanumeric excludes it, so a match has one split.
_POSIX = re.compile(r"^/(?!/)[^\x00-\x1fA-Za-z0-9]*[A-Za-z0-9][^\x00-\x1f]*")
_WINDOWS = re.compile(r"^[A-Za-z]:[\\/][^\x00-\x1fA-Za-z0-9]*[A-Za-z0-9][^\x00-\x1f]*")
_UNC = re.compile(r"^\\\\[^\\\x00-\x1f]+\\[^\x00-\x1f]+")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]{0,63}://")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _host(t: str) -> Optional[str]:
    if _is_ip(t):
        return None
    # A name carries a letter, so a malformed address is not read as one.
    if _HOST.fullmatch(t) and re.search(r"[A-Za-z]", t):
        return None
    hexes = sum(c in string.hexdigits for c in t)
    if _DEVICE.fullmatch(t) and (
        (re.search(r"[A-Za-z]", t) and re.search(r"\d", t)) or hexes >= 32
    ):
        return None
    return "target_not_a_host"


def _ip(t: str) -> Optional[str]:
    # A bare prefix length only (no netmask or scope), so one network has one key;
    # strict: a prefix with host bits set is refused, not quietly rewritten.
    if not re.fullmatch(r"[0-9A-Fa-f.:]+(/\d{1,3})?", t):
        return "target_not_an_address"
    try:
        ipaddress.ip_network(t, strict=True)
        return None
    except ValueError:
        return "target_not_an_address"


def _domain(t: str) -> Optional[str]:
    labels = t.split(".")
    if (
        len(labels) >= 2
        and all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        and _TLD.fullmatch(labels[-1])
    ):
        return None
    return "target_not_a_domain"


def _file(t: str) -> Optional[str]:
    if _URL.match(t):
        return "target_not_a_path_or_hash"
    if any(p.fullmatch(t) for p in (_HASH, _POSIX, _WINDOWS, _UNC)):
        return None
    return "target_not_a_path_or_hash"


def _user(t: str) -> Optional[str]:
    if any(p.fullmatch(t) for p in (_UPN, _DOWN_LEVEL, _UUID, _SID, _ACCOUNT)):
        return None
    return "target_not_a_user"


def _custom(t: str) -> Optional[str]:
    return None


# action_type -> (length limit, shape check)
TARGET_RULES: Dict[str, Tuple[int, Callable[[str], Optional[str]]]] = {
    "isolate_host": (253, _host),
    "block_ip": (64, _ip),
    "block_domain": (253, _domain),
    "quarantine_file": (4096, _file),
    "disable_user": (256, _user),
    "revoke_session": (256, _user),
    "custom": (1000, _custom),
}


def check_target(action_type: str, target: Any) -> Tuple[Optional[str], Optional[str]]:
    """(stripped target, None) when it fits ``action_type``, else (None, code)."""
    if not isinstance(action_type, str) or action_type not in TARGET_RULES:
        return None, "action_type_unknown"
    if not isinstance(target, str) or not target.strip():
        return None, "target_missing"
    t = target.strip()
    # Template markers first, while <hostname> still reads as one.
    if any(m in t.lower() for m in _TEMPLATE_MARKERS):
        return None, "target_is_placeholder"
    bare = " ".join(t.strip(_EDGE).lower().split())
    if bare in _NON_ANSWERS or not any(c.isalnum() for c in bare):
        return None, "target_is_placeholder"
    limit, shape = TARGET_RULES[action_type]
    if len(t) > limit:
        return None, "target_too_long"
    code = shape(t)
    return (None, code) if code else (t, None)


def check_confidence(raw: Dict[str, Any]) -> Optional[str]:
    """None when ``raw`` states a confidence in (0, 1], else the reason code.

    Absent is not zero: a model that stated no view is told apart from one that
    stated none.
    """
    if "confidence" not in raw or raw["confidence"] is None:
        return "confidence_missing"
    c = raw["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or c != c or c > 1:
        return "confidence_out_of_range"
    if c <= 0:
        return "confidence_zero"
    return None


# A word for what a refused target looks like, so the case says what kind of
# value was refused without carrying the value itself.
def target_kind(target: Any) -> str:
    if not isinstance(target, str) or not target.strip():
        return "missing"
    t = target.strip()
    # Longer than any action allows: name it without reading it.
    if len(t) > max(limit for limit, _ in TARGET_RULES.values()):
        return "oversized"
    bare = " ".join(t.strip(_EDGE).lower().split())
    if (
        any(m in t.lower() for m in _TEMPLATE_MARKERS)
        or bare in _NON_ANSWERS
        or not any(c.isalnum() for c in bare)
    ):
        return "placeholder"
    if _URL.match(t):
        return "url"
    if _is_ip(t):
        return "address"
    if "/" in t and _ip(t) is None:
        return "network"
    if _HASH.fullmatch(t):
        return "hash"
    if any(p.fullmatch(t) for p in (_POSIX, _WINDOWS, _UNC)):
        return "path"
    if _UPN.fullmatch(t):
        return "email"
    if _DOWN_LEVEL.fullmatch(t):
        return "account"
    if _UUID.fullmatch(t) or _SID.fullmatch(t):
        return "object_id"
    if _domain(t) is None:
        return "domain"
    if _HOST.fullmatch(t) and "." not in t:
        return "single_label"
    if any(c.isspace() for c in t):
        return "text"
    return (
        "not_an_address"
        if re.fullmatch(r"[0-9A-Fa-f./:]+", t) and re.search(r"[.:]", t)
        else "token"
    )


# ---- Containment: who carries out an approved containment (#5059, #5234) ----
# Per kind, each in-repo server that can: the lookup that turns the target into
# the vendor's ID, the act, the state read and the undo (#5060 builds the way
# back). Tool names are bare; the server prefixes them as the registry does.
# ``rows`` names the list a lookup answers in, None for a single record.


def _host_lookup(target: str) -> Dict[str, str]:
    return {"ip": target} if _is_ip(target) else {"hostname": target}


def _mde_state(machine: Dict[str, Any]) -> str:
    isolation = machine.get("isolation") or {}
    if isolation.get("pending"):
        return "requested"
    return "contained" if isolation.get("state") == "isolated" else "not_contained"


# A machine action's status, as Defender words it.
_MDE_ACTION_STATES = {
    "Pending": "requested",
    "InProgress": "requested",
    "Succeeded": "contained",
}


def _cf_state(done: str) -> Callable[[Dict[str, Any]], str]:
    # Cloudflare answers a refusal with success false and no error key, which
    # _call does not read as a failure: only success true is done.
    return lambda answer: done if answer.get("success") is True else "failed"


CONTAINMENT_TOOLS: Dict[str, Tuple[Dict[str, Any], ...]] = {
    "isolate_host": (
        {
            "vendor": "microsoft-defender",
            "lookup": "mde_find_machine",
            "lookup_args": _host_lookup,
            "rows": "machines",
            "id": "machine_id",
            "name": "name",
            "state": _mde_state,
            "act": "mde_isolate",
            "id_arg": "machine_id",
            "note_arg": "comment",
            # The act answers a machine action; its status is the state.
            "act_state": lambda answer: _MDE_ACTION_STATES.get(
                answer.get("status"), "failed"
            ),
            "state_read": ("mde_get_machine_action", "action_id"),
            "read_state": lambda answer: _MDE_ACTION_STATES.get(
                answer.get("status"), "failed"
            ),
            "undo": "mde_release",
        },
        {
            "vendor": "carbon-black",
            "lookup": "cb_search_device",
            "lookup_args": _host_lookup,
            "rows": "devices",
            "id": "device_id",
            "name": "name",
            "state": lambda d: "contained" if d.get("quarantined") else "not_contained",
            "act": "cb_quarantine",
            "id_arg": "device_id",
            "note_arg": "reason",
            "act_state": lambda answer: "requested",
            # No action read: the lookup's quarantine flag says when it holds.
            "state_read": None,
            "undo": "cb_lift_quarantine",
        },
    ),
    "disable_user": (
        {
            "vendor": "azure-ad",
            "lookup": "aad_get_user",
            "lookup_args": lambda target: {"user": target},
            "rows": None,
            "id": "id",
            "name": "userPrincipalName",
            "state": lambda u: "enabled" if u.get("accountEnabled") else "disabled",
            "act": "aad_disable_user",
            "id_arg": "user_id",
            "note_arg": "reason",
            "act_state": lambda answer: "requested",
            "state_read": None,
            "undo": "aad_enable_user",
        },
    ),
    # Keyless (#5234): the target is the vendor's key, so there is no lookup and
    # one candidate. ``takes`` refuses a target the act can't carry, with the
    # sentence the row shows; ``undo_from`` names the field of the act's answer
    # the undo is called with (under the same name).
    "block_ip": (
        {
            "vendor": "cloudflare",
            "carrier": "Cloudflare WAF",
            "lookup": None,
            "takes": lambda t: (
                None
                if _is_ip(t)
                else "Cloudflare's WAF takes one address, not a network"
            ),
            "act": "cf_waf_block_ip",
            "id_arg": "ip",
            "note_arg": "reason",
            "act_state": _cf_state("blocked"),
            "state_read": None,
            "undo": "cf_waf_unblock_ip",
            "undo_from": "rule_id",
        },
        # No ``undo_from``: the unregister takes the address itself.
        {
            "vendor": "palo-alto",
            "carrier": "Palo Alto",
            "lookup": None,
            "takes": lambda t: (
                None if _is_ip(t) else "Palo Alto takes one address, not a network"
            ),
            "act": "pan_block_ip",
            "id_arg": "ip",
            "note_arg": "reason",
            "act_state": _cf_state("blocked"),
            "state_read": None,
            "undo": "pan_unblock_ip",
        },
    ),
    "block_domain": (
        {
            "vendor": "cloudflare",
            "carrier": "Cloudflare Gateway",
            "lookup": None,
            # The domain goes into the rule's expression: only a bare name passes.
            "takes": lambda t: (
                None if _domain(t) is None else "Cloudflare Gateway takes a domain name"
            ),
            "act": "cf_gateway_block_domain",
            "id_arg": "domain",
            "note_arg": "reason",
            "act_state": _cf_state("blocked"),
            "state_read": None,
            "undo": "cf_gateway_unblock_domain",
            "undo_from": "rule_id",
        },
    ),
    "revoke_session": (
        {
            "vendor": "cloudflare",
            "carrier": "Cloudflare Access",
            "lookup": None,
            "takes": lambda t: (
                None
                if _UPN.fullmatch(t)
                else "Cloudflare Access takes an email address, not another account name"
            ),
            "act": "cf_access_revoke_session",
            "id_arg": "email",
            "note_arg": "reason",
            "act_state": _cf_state("revoked"),
            "state_read": None,
            "undo": None,
        },
    ),
}

# What the act is for, in the state words above.
DONE_STATE = {
    "isolate_host": "contained",
    "disable_user": "disabled",
    "block_ip": "blocked",
    "block_domain": "blocked",
    "revoke_session": "revoked",
}
# The way back (#5060): what a row carrying ``undoes`` records once the vendor
# reports it, and the lookup's word (``state`` above) for that state. A kind
# with no entry here has no undo.
UNDONE_STATE = {
    "isolate_host": "released",
    "disable_user": "enabled",
    "block_ip": "unblocked",
    "block_domain": "unblocked",
}
UNDONE_LOOKUP = {"isolate_host": "not_contained", "disable_user": "enabled"}
# The undo's verb on the case and in its row's title.
UNDO_VERBS = {
    "isolate_host": "Release",
    "disable_user": "Enable",
    "block_ip": "Unblock",
    "block_domain": "Unblock",
}


def undone(action_type: str, execution_result: Optional[Dict[str, Any]]) -> bool:
    """An executed act undone since, so no longer in force: it answers no new
    ask of the same act on the same target."""
    result = execution_result or {}
    state = UNDONE_STATE.get(action_type)
    return bool(result.get("undone_by")) or (
        state is not None and result.get("state") == state
    )
