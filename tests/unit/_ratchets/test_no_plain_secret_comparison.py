import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Production Python only. Test trees assert against expected values all day
# and hold no inbound secret check, so anything under a tests/ directory or
# named test_*.py is out of scope.
SCANNED_DIRS = (_REPO_ROOT / "core", _REPO_ROOT / "services")

# A name reads as secret-like when it names a credential, a token, a key, or
# the expected side of a check. A plain ==/!= against one of these is how a
# caller-presented secret ends up compared in variable time: != returns on
# the first differing character, compare_digest does not.
SECRET_NAME = re.compile(
    r"(token|secret|passw|credential|expected|signature|api_?key|auth_?key|_key$|^key$)",
    re.IGNORECASE,
)
_KEY_FAMILY = re.compile(r"(_key$|^key$)", re.IGNORECASE)

# (file, stripped source line) -> why no caller-presented value reaches the
# comparison. An exemption is a claim a reader can check, not a skip: each
# one says what the two sides actually are. The tests below hold the list
# honest both ways — an unlisted new comparison fails, and so does a listed
# line that no longer matches anything the scan flags.
EXEMPT = {
    ("core/audit/trail.py", "if prev_hash != expected:"): (
        "both hashes are server-side: one is stored on the previous row, "
        "the other computed from it"
    ),
    ("core/auth/auth_service.py", 'if token_type == "access"'): (
        "a JWT claim value, not a secret; the signature is verified before "
        "claims are read"
    ),
    ("core/auth/current_user.py", 'if payload.get("token_type") != ACCESS_TOKEN_TYPE:'): (
        "a JWT claim value, not a secret; the signature is verified before "
        "claims are read"
    ),
    ("core/auth/mcp_credential_service.py", ".filter(McpCredential.credential_id == credential_id)"): (
        "a SQLAlchemy filter expression: == builds a SQL clause and no "
        "values are compared in Python"
    ),
    ("core/auth/mcp_credential_service.py", ".filter(McpCredential.token_hash == _hash(token))"): (
        "the presented token is hashed first and the database compares "
        "hashes: hash-then-lookup, not a secret comparison"
    ),
    ("core/backup/create.py", "if named != expected:"): (
        "sets of backup location names, one from the manifest and one from "
        "the filesystem; both server-side"
    ),
    ("core/detections/tools.py", "or actual != expected"): (
        "detection-rule condition operands: an event field and the rule "
        "author's expected value; neither is a credential"
    ),
    ("core/integrations/mcp/verify_catalog.py", "if actual != expected:"): (
        "the packaged connector names against the servers the service "
        "reports; both server-side"
    ),
    ("core/memory/distil.py", "EpisodicDistilFailure.failure_key == key.value,"): (
        "a SQLAlchemy filter expression over a failure-category key: == "
        "builds a SQL clause and no values are compared in Python"
    ),
    ("core/memory/recall.py", "if field == _PRIMARY_KEY:"): (
        "a field name against the primary-key field-name constant; the "
        "value compared is a column name"
    ),
    ("core/response/approval_service.py", ".where(ApprovalActionRow.idempotency_key == key)"): (
        "a SQLAlchemy filter expression; the idempotency key is a "
        "deduplication token the database compares, not a credential "
        "verified in Python"
    ),
    ("core/secrets_manager.py", "if self._fernet is None or key != self._fernet_key:"): (
        "the master key read from disk against its own in-memory cache; "
        "both server-side and no caller presents either"
    ),
    ("core/skills/skill_library.py", "if expected_version != _version_of(existing):"): (
        "a skill version string, not a secret"
    ),
    ("services/api/main.py", 'if _status["write_backend"] != _status["expected_write_backend"]:'): (
        "the service's own status fields: the write backend it resolved "
        "against the one it expected; configuration state, not a credential"
    ),
    ("services/api/mcp_surface.py", "if token != DEV_MODE_TOKEN:"): (
        "DEV_MODE_TOKEN is a placeholder published in that module and "
        "honoured only when dev mode is on; there is no deployment secret "
        "to time"
    ),
    ("services/api/middleware/auth_rate_key.py", 'if payload.get("token_type") != expected_type:'): (
        "a JWT claim value, not a secret; the signature is verified before "
        "claims are read"
    ),
    ("services/api/routers/auth.py", 'if not payload or payload.get("token_type") != "refresh":'): (
        "a JWT claim value, not a secret; the signature is verified before "
        "claims are read"
    ),
    ("services/api/routers/config.py", 'if config.webhook_secret is not None and config.webhook_secret != "":'): (
        "an emptiness check on the stored value against a literal; no "
        "presented value is compared"
    ),
    ("services/daemon/threat_feed_poller.py", "if missing == _last_skip_signature:"): (
        "the names of missing configuration keys against the previous "
        "poll's set, so a warning repeats only when the set changes"
    ),
    ("services/medic/contracts/pack_check.py", 'if expected != m["counts"]:'): (
        "contract-pack counts declared in a manifest against the counts "
        "found; both are file contents, not credentials"
    ),
    ("services/medic/contracts/pack_check.py", 'if entry["kind"] != expected_kind:'): (
        "a contract entry's kind label against the expected kind; an enum, "
        "not a secret"
    ),
    ("core/detections/detection_rules_service.py", "self._rules_dir_key("): (
        "the rules-directory key derived from a source's path and "
        "subdirectory, compared with the same derivation for another "
        "configured source; an identity key for a directory, not a "
        "credential"
    ),
    ("services/medic/engine/history.py", 'if v["key"] == spec["key"]'): (
        "the key field of two history records: it names an observation "
        "series in a data file, not a secret"
    ),
    ("services/medic/redact/rules.py", "if not password or password == REDACTED:"): (
        "the password parsed out of a URL being redacted, compared with "
        "the public redaction marker so an already-redacted value is left "
        "alone"
    ),
    ("services/medic_gateway/session.py", "if token != self.access:"): (
        "the gateway's own access token against the one the backend just "
        "rejected; it decides whether the refused token is still the "
        "current one and verifies no presented secret"
    ),
    ("services/medic_gateway/session.py", "if token == self.access:"): (
        "the gateway's own access token against the one the backend just "
        "rejected; it decides whether the refused token is still the "
        "current one and verifies no presented secret"
    ),
}


def _operand_names(node):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        yield str(node.slice.value)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        yield str(node.args[0].value)


def _flagged_lines(source: str):
    """Line numbers of plain ==/!= comparisons against a secret-like name."""
    flagged = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        names = [name for operand in operands for name in _operand_names(operand)]
        secret_like = [name for name in names if SECRET_NAME.search(name)]
        if not secret_like:
            continue
        # A comparison against a numeric literal is a count or a code, never
        # a secret value.
        if any(
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, (int, float))
            and not isinstance(operand.value, bool)
            for operand in operands
        ):
            continue
        # A key-family name compared with a string literal is dictionary or
        # configuration key dispatch; a secret's value never appears as a
        # literal in the source.
        if all(_KEY_FAMILY.search(name) for name in secret_like) and any(
            isinstance(operand, ast.Constant) and isinstance(operand.value, str)
            for operand in operands
        ):
            continue
        flagged.append(node.lineno)
    return flagged


def _is_test_path(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def _scanned_files():
    return [
        path
        for directory in SCANNED_DIRS
        for path in directory.rglob("*.py")
        if not _is_test_path(path.relative_to(_REPO_ROOT))
    ]


def _flagged_sites():
    sites = set()
    for path in _scanned_files():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for lineno in _flagged_lines(source):
            sites.add(
                (str(path.relative_to(_REPO_ROOT)), lines[lineno - 1].strip())
            )
    return sites


@pytest.mark.unit
def test_no_plain_comparison_against_a_secret_like_name():
    violations = sorted(_flagged_sites() - EXEMPT.keys())
    assert not violations, (
        "A plain ==/!= against a secret-like name compares in variable "
        "time. Use hmac.compare_digest (on UTF-8-encoded values, so a "
        "non-ASCII presented value is a refusal rather than an error), or "
        "add an EXEMPT entry here saying why no caller-presented value "
        "reaches the comparison.\n"
        + "\n".join(f"{path}: {line}" for path, line in violations)
    )


@pytest.mark.unit
def test_exemptions_still_match_flagged_lines():
    """An exemption for a line that no longer matches is a stale claim."""
    stale = sorted(EXEMPT.keys() - _flagged_sites())
    assert not stale, (
        "These EXEMPT entries no longer match anything the scan flags — "
        "the comparison was fixed, moved or reworded, so the exemption and "
        "its reason are out of date.\n"
        + "\n".join(f"{path}: {line}" for path, line in stale)
    )


@pytest.mark.unit
def test_the_scan_flags_a_plain_secret_comparison():
    """The guard above is only as good as its pattern.

    A scan that flags nothing passes every test while guarding nothing, so
    feed it the shape it exists to catch, the fix it should accept, and the
    key-dispatch shape it deliberately leaves alone.
    """
    plain = (
        "def check(token, expected):\n"
        "    if token != expected:\n"
        "        return False\n"
        "    return True\n"
    )
    assert _flagged_lines(plain) == [2]

    constant_time = (
        "import hmac\n"
        "def check(token, expected):\n"
        "    return hmac.compare_digest(token.encode(), expected.encode())\n"
    )
    assert _flagged_lines(constant_time) == []

    key_dispatch = (
        "def pick(key):\n"
        '    return key == "metadata"\n'
    )
    assert _flagged_lines(key_dispatch) == []


@pytest.mark.unit
def test_the_scan_reaches_production_code():
    scanned = _scanned_files()
    assert len(scanned) > 300, f"scan path covers almost nothing: {len(scanned)} files"
    assert any(path.name == "vstrike.py" for path in scanned), (
        "services/api/routers/vstrike.py is not in the scan path — the "
        "receiver this ratchet was written for is no longer guarded"
    )
