"""A secret is compared in constant time, never with ``==`` / ``!=``.

A plain comparison returns at the first differing character, so the time it
takes tells a caller how much of a guessed key was right. Inbound shared-secret
checks use ``hmac.compare_digest`` on bytes (see ``core/agents/internal_auth.py``
and ``services/api/routers/vstrike.py``). This flags ``==`` / ``!=`` where either
side is named like a secret. A comparison against a literal is not flagged: the
literal is already in the source. Test code is not scanned: no caller reaches it.

An exception goes in ALLOWED with a one-line reason: no caller-presented value
reaches the comparison, or neither side is a secret.
"""

import ast
import functools
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services")

_SECRET_WORDS = {
    "token",
    "key",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "apikey",
    "signature",
    "expected",
}

# (file, comparison as ast.unparse writes it) -> why it is not a secret check.
ALLOWED = {
    ("core/audit/trail.py", "prev_hash != expected"): (
        "hash chain check: both sides read from the audit_trail table"
    ),
    ("core/auth/current_user.py", "payload.get('token_type') != ACCESS_TOKEN_TYPE"): (
        "claim of a JWT whose signature is already verified, against a public type name"
    ),
    (
        "core/auth/mcp_credential_service.py",
        "McpCredential.token_hash == _hash(token)",
    ): "SQL lookup by the token's hash; the secret itself is never compared",
    ("core/backup/create.py", "named != expected"): (
        "location names from the backup manifest against the configured ones"
    ),
    ("core/backup/restore.py", "key_path.read_bytes() != key_bytes"): (
        "master.key read twice from disk to catch a rewrite; no caller value"
    ),
    (
        "core/detections/detection_rules_service.py",
        "self._rules_dir_key(source['local_path'], source.get('subdirectory', '')) == key",
    ): "rules-directory paths, not secrets",
    ("core/detections/tools.py", "actual != expected"): (
        "event field against a detection rule's value; neither is a secret"
    ),
    ("core/integrations/mcp/verify_catalog.py", "actual != expected"): (
        "server names in mcp-config.json against the loaded catalog"
    ),
    ("core/memory/distil.py", "EpisodicDistilFailure.failure_key == key.value"): (
        "SQL filter on a failure's subject key, an identifier"
    ),
    ("core/memory/recall.py", "field == _PRIMARY_KEY"): (
        "sort-term name against a constant field name"
    ),
    ("core/response/approval_service.py", "ApprovalActionRow.idempotency_key == key"): (
        "SQL filter on an idempotency key, an identifier"
    ),
    ("core/secrets_manager.py", "key != self._fernet_key"): (
        "master.key from disk against the cached copy; no caller value"
    ),
    ("core/skills/skill_library.py", "expected_version != _version_of(existing)"): (
        "the caller's expected skill version number, not a secret"
    ),
    ("services/api/mcp_surface.py", "token != DEV_MODE_TOKEN"): (
        "DEV_MODE_TOKEN is a public literal in this file, so timing reveals nothing"
    ),
    (
        "services/api/middleware/auth_rate_key.py",
        "payload.get('token_type') != expected_type",
    ): "claim of a JWT whose signature is already verified, against a public type name",
    ("services/daemon/threat_feed_poller.py", "missing == _last_skip_signature"): (
        "tuple of missing config names, kept to warn once"
    ),
    ("services/medic/contracts/pack_check.py", "entry['kind'] != expected_kind"): (
        "pack manifest kind against the file's location"
    ),
    ("services/medic/contracts/pack_check.py", "expected != m['counts']"): (
        "pack manifest counts against the files present"
    ),
    ("services/medic/redact/rules.py", "password == REDACTED"): (
        "checks whether a URL password is already the public redaction marker"
    ),
    ("services/medic_gateway/session.py", "token != self.access"): (
        "the gateway's own outbound token against its cached copy; no caller value"
    ),
    ("services/medic_gateway/session.py", "token == self.access"): (
        "the gateway's own outbound token against its cached copy; no caller value"
    ),
}


def _name(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _secret_like(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):  # f"Bearer {expected}"
        return any(
            _secret_like(part.value)
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    name = _name(node)
    if not name:
        return False
    words = re.split(r"_|(?<=[a-z])(?=[A-Z])", name)
    return any(word.lower() in _SECRET_WORDS for word in words)


def _plain_secret_compares(source: str):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, operands, operands[1:]):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            if isinstance(left, ast.Constant) or isinstance(right, ast.Constant):
                continue
            if _secret_like(left) or _secret_like(right):
                yield node.lineno, ast.unparse(node)
                break


def _scanned():
    for package in PACKAGES:
        for path in sorted((_REPO_ROOT / package).rglob("*.py")):
            rel = path.relative_to(_REPO_ROOT)
            if {"__pycache__", "node_modules", "tests"} & set(rel.parts):
                continue
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            yield rel


@functools.cache
def _found():
    return [
        (rel.as_posix(), lineno, text)
        for rel in _scanned()
        for lineno, text in _plain_secret_compares(
            (_REPO_ROOT / rel).read_text(encoding="utf-8")
        )
    ]


@pytest.mark.unit
def test_no_plain_secret_comparison():
    violations = [
        f"{rel}:{lineno}: {text}"
        for rel, lineno, text in _found()
        if (rel, text) not in ALLOWED
    ]
    assert not violations, (
        "A secret-like value is compared with ==/!=, which leaks how much of a "
        "guess matched. Encode both sides to bytes and use hmac.compare_digest. "
        "If no caller-presented value reaches it, or neither side is a secret, add "
        "it to ALLOWED with the reason.\n" + "\n".join(violations)
    )


@pytest.mark.unit
def test_every_allowed_entry_still_exists():
    """An entry whose code is gone would let the same text back in unreviewed."""
    found = {(rel, text) for rel, _, text in _found()}
    stale = sorted(
        f"{rel}: {text}" for rel, text in ALLOWED if (rel, text) not in found
    )
    assert not stale, "Remove these from ALLOWED:\n" + "\n".join(stale)


@pytest.mark.unit
def test_the_scan_flags_a_plain_compare_and_not_a_constant_time_one():
    flagged = list(
        _plain_secret_compares(
            "def check(token, expected, api_key, inboundKey, header):\n"
            "    if token != expected:\n"
            "        return False\n"
            "    if header != f'Bearer {expected}':\n"
            "        return False\n"
            "    return api_key == inboundKey\n"
        )
    )
    assert flagged == [
        (2, "token != expected"),
        (4, "header != f'Bearer {expected}'"),
        (6, "api_key == inboundKey"),
    ]

    assert not list(
        _plain_secret_compares(
            "def check(token, expected):\n"
            "    if token is None or token == '':\n"
            "        return False\n"
            "    return hmac.compare_digest(token.encode(), expected.encode())\n"
        )
    )


@pytest.mark.unit
def test_the_scan_reaches_the_inbound_receivers():
    scanned = {rel.as_posix() for rel in _scanned()}
    assert len(scanned) > 200, f"scan covers almost nothing: {len(scanned)} files"
    assert "services/api/routers/vstrike.py" in scanned
    assert "core/agents/internal_auth.py" in scanned
