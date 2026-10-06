"""scripts/vigil-support/redact.awk must leave no known secret in a support bundle.

A sentinel is planted for every registered integration secret in each surface a
bundle collects, run through every awk on the host, and must not survive. A
fixed set of non-secret lines goes through the same invocation and must come
back byte-identical.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.integrations.integration_secrets import INTEGRATION_SECRET_FIELDS

pytestmark = pytest.mark.unit

SUPPORT = Path(__file__).resolve().parents[2] / "scripts" / "vigil-support"
REDACT = SUPPORT / "redact.awk"
NAMES = SUPPORT / "secret-names.txt"

# gawk and mawk both, when installed; `awk` is usually one of them.
AWKS = sorted(
    {
        os.path.realpath(path): name
        for name in ("gawk", "mawk", "awk")
        if (path := shutil.which(name))
    }.values()
)

FIELDS = [
    (integration, field, env)
    for integration, fields in sorted(INTEGRATION_SECRET_FIELDS.items())
    for field, env in sorted(fields.items())
]

CONTROLS = [
    "AUTH_MIN_PASSWORD_LENGTH=12",
    'AUTH_MIN_PASSWORD_LENGTH="12"',
    "# AUTH_MAX_PASSWORD_BYTES=72",
    "PASSWORD_RESET_TTL_SECONDS: 3600",
    "VIGIL_RUN_ID=7f3c2a10-9d4e-4b8a-a1c5-0e6d2b9f4a37",
    "image_digest: sha256:" + "9f86d081884c7d659a2feaa0c55ad015" * 2,
    "CORS_ORIGINS=http://localhost:6988,https://vigil.example.com/app",
    "2026-10-05 12:00:00,123 - vigil.daemon - INFO - polled https://api.example.com/v1/x",
    'INFO:     10.0.0.5:4242 - "GET /api/health HTTP/1.1" 200 OK',
    "git+ssh://git@github.com/Vigil-SOC/vigil.git",
    "docker.elastic.co/elasticsearch/elasticsearch:8.15.0 deeptempo-elasticsearch elastic_data:",
    '"elastic": { "args": ["core/integrations/elastic/tool.py"] }',
    "worker monitor reported 3 events",
    "/usr/bin/vigil --port 6987 --log-level info --workers 4",
    "image: sha256:" + "0123456789abcdef" * 4,
]


def run_awk(awk, text, values=(), counts=None, names=NAMES, extra=()):
    args = [awk, "-f", str(REDACT), "-v", f"names={names}", *extra]
    if values:
        path = counts.parent / "values.txt"
        path.write_text("\n".join(values) + "\n")
        args += ["-v", f"values={path}"]
    if counts:
        args += ["-v", f"counts={counts}", "-v", "name=probe"]
    return subprocess.run(
        args, input=text.encode(), capture_output=True, check=True
    ).stdout.decode()


def camel(env):
    head, *rest = env.lower().split("_")
    return head + "".join(word.capitalize() for word in rest)


def surfaces(env, secret):
    return [
        f"{env}={secret}",
        f'export {env}="{secret}"',
        f"services:\n  api:\n    environment:\n      {env}: {secret}\n      - {env}={secret}",
        f'  {camel(env)}: "{secret}"',
        f'{{"{env.lower()}": "{secret}", "debug": false}}',
        f"postgresql://vigil:{secret}@db.internal:5432/vigil",
        f'INFO:     10.0.0.5:4242 - "GET /api/config?{env}={secret} HTTP/1.1" 200 OK',
        f"2026-10-05 12:00:00,123 - vigil.daemon - ERROR - connector failed {env}={secret}",
        f'[agent] worker 3 call failed: {{"{env}":"{secret}"}} retrying',
        f"- name: {env}\n  value: {secret}",
    ]


@pytest.mark.parametrize("awk", AWKS)
@pytest.mark.parametrize(("integration", "field", "env"), FIELDS)
def test_planted_secret_is_gone_and_controls_survive(awk, integration, field, env):
    secret = f"s3ntinel-{integration}-{field}-Qx7"
    text = "\n".join(surfaces(env, secret) + CONTROLS) + "\n"
    out = run_awk(awk, text)
    assert secret not in out
    assert out.count("[REDACTED]") >= len(surfaces(env, secret))
    assert gzip.decompress(gzip.compress(out.encode())).decode() == out
    for line in CONTROLS:
        assert line in out.splitlines()


@pytest.mark.parametrize("awk", AWKS)
def test_exact_values_are_replaced_as_fixed_strings(awk, tmp_path):
    counts = tmp_path / "counts.tsv"
    text = "login failed for a.b*c+d9\nretry a.b*c+d9 and axbbbcd9\nshort abc12 ok\n"
    out = run_awk(awk, text, values=["a.b*c+d9", "abc12"], counts=counts)
    assert out == (
        "login failed for [REDACTED]\nretry [REDACTED] and axbbbcd9\nshort abc12 ok\n"
    )
    assert counts.read_text() == "probe\t2\n"


@pytest.mark.parametrize("awk", AWKS)
def test_exact_values_replace_only_where_they_stand_alone(awk, tmp_path):
    counts = tmp_path / "counts.tsv"
    text = (
        "pw=hunter2hunter2 hunter2hunter2x xhunter2hunter2 hunter2hunter2.\n"
        "end p@ssw0rd!word,p@ssw0rd!\n"
        'postgresql://vigil:hunter2hunter2@db and {"a":"x\\nhunter2hunter2"}\n'
    )
    out = run_awk(awk, text, values=["hunter2hunter2", "p@ssw0rd!"], counts=counts)
    assert out == (
        "pw=[REDACTED] hunter2hunter2x xhunter2hunter2 [REDACTED].\n"
        "end [REDACTED]word,[REDACTED]\n"
        'postgresql://vigil:[REDACTED]@db and {"a":"x\\n[REDACTED]"}\n'
    )
    assert counts.read_text() == "probe\t6\n"


@pytest.mark.parametrize("awk", AWKS)
def test_longest_value_wins_and_replacement_is_not_rescanned(awk, tmp_path):
    counts = tmp_path / "counts.tsv"
    out = run_awk(
        awk,
        "a hunter2hunter2-long b hunter2hunter2 c\n",
        values=["hunter2hunter2", "hunter2hunter2-long", "REDACTED]x"],
        counts=counts,
    )
    assert out == "a [REDACTED] b [REDACTED] c\n"
    assert counts.read_text() == "probe\t2\n"


FLAG_CASES = [
    (
        "ps -ef: vigil --auth-token " + "0123456789abcdef" * 4 + " --port 6987",
        "ps -ef: vigil --auth-token [REDACTED] --port 6987",
    ),
    (
        "vigil --api-key=abc123def456 --log-level info",
        "vigil --api-key=[REDACTED] --log-level info",
    ),
    (
        "vigil --password hunter2 --log-level info",
        "vigil --password [REDACTED] --log-level info",
    ),
    ("vigil --db-password 'a b' x", "vigil --db-password '[REDACTED]' x"),
    ("vigil --token --verbose", "vigil --token --verbose"),
    ("vigil --token", "vigil --token"),
    ("vigil --password [REDACTED] --x", "vigil --password [REDACTED] --x"),
    (
        "vigil -p hunter2hunter2 --max-tokens 4096",
        "vigil -p hunter2hunter2 --max-tokens 4096",
    ),
    ("a--token hunter2", "a--token hunter2"),
]


@pytest.mark.parametrize("awk", AWKS)
@pytest.mark.parametrize(("line", "expected"), FLAG_CASES)
def test_long_credential_flags(awk, line, expected, tmp_path):
    counts = tmp_path / "counts.tsv"
    assert run_awk(awk, line + "\n", counts=counts) == expected + "\n"
    assert counts.read_text() == (
        f"probe\t{expected.count('[REDACTED]') - line.count('[REDACTED]')}\n"
    )


@pytest.mark.parametrize("awk", AWKS)
def test_learn_prints_only_credential_class_values(awk):
    text = "\n".join(
        [
            '# ELASTIC_SIEM_USERNAME="elastic"',
            "  # POSTGRES_PASSWORD=commentedpw",
            'ELASTIC_SIEM_USERNAME="elastic"',
            'SPLUNK_USERNAME="admin"',
            "POSTGRES_PASSWORD=hunter2hunter2",
            "SERVICE_API_KEY=short",
            'SERVICE_API_KEY: "quoted-key-value"',
            "DATABASE_URL=postgresql://vigil:urlpassword@db/vigil",
            "- name: SMTP_PASSWORD\n  value: from-name-value",
            "- name: SPLUNK_USERNAME\n  value: not-learned-user",
            "vigil --auth-token flagtokenvalue",
        ]
    )
    out = run_awk(awk, text + "\n", extra=("-v", "learn=1"))
    assert out.splitlines() == [
        "hunter2hunter2",
        "quoted-key-value",
        "urlpassword",
        "from-name-value",
        "flagtokenvalue",
    ]


@pytest.mark.parametrize("awk", AWKS)
def test_empty_values_stay_empty_and_uncounted(awk, tmp_path):
    counts = tmp_path / "counts.tsv"
    text = 'API_KEY=\nJWT_SECRET_KEY=""\n"token": ""\npassword: ""\nAUTH_TOKEN=null\n'
    assert run_awk(awk, text, counts=counts) == text
    assert counts.read_text() == "probe\t0\n"


@pytest.mark.parametrize("awk", AWKS)
def test_one_replacement_per_secret(awk, tmp_path):
    counts = tmp_path / "counts.tsv"
    key = "sk-ant-api03-" + "A1b2C3d4" * 4
    out = run_awk(
        awk, f"ANTHROPIC_API_KEY={key}\nAuthorization: Bearer {key}\n", counts=counts
    )
    assert out == "ANTHROPIC_API_KEY=[REDACTED]\nAuthorization: [REDACTED]\n"
    assert counts.read_text() == "probe\t2\n"


@pytest.mark.parametrize("awk", AWKS)
def test_run_together_names_are_caught(awk):
    out = run_awk(awk, "PGPASSWORD: hunter2hunter2\nmonkey: banana\n")
    assert out == "PGPASSWORD: [REDACTED]\nmonkey: banana\n"


EDGE_CASES = [
    ("POSTGRES_PASSWORD={hunter2hunter2", "POSTGRES_PASSWORD=[REDACTED]"),
    (
        'x password=SecretStr("hunter2hunter2") y',
        'x password=SecretStr("[REDACTED]") y',
    ),
    ("x password: 'it''s secret' y", "x password: '[REDACTED]' y"),
    ("x PGPASSWORD=ab;cd y", "x PGPASSWORD=[REDACTED] y"),
    ('{"t": "a\\nghp_abcdefghijklmnopqrstuvwxyz0123"}', '{"t": "a\\n[REDACTED]"}'),
    ("db=postgres://u:abc/def==@h/x", "db=postgres://u:[REDACTED]@h/x"),
    (
        "https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/x",
        "https://x-access-token:[REDACTED]@github.com/x",
    ),
    ("http://host:8080/p?email=a@b.com", "http://host:8080/p?email=a@b.com"),
    (
        "Using Bearer authentication for upstream",
        "Using Bearer authentication for upstream",
    ),
    ("Authorization: Bearer [REDACTED]", "Authorization: Bearer [REDACTED]"),
    (
        "- name: SMTP_PASSWORD\n\n  value: hunter2hunter2",
        "- name: SMTP_PASSWORD\n\n  value: [REDACTED]",
    ),
    (
        "  - password: |\n      secret\n    other: 1",
        "  - password: |\n      [REDACTED]\n    other: 1",
    ),
]


@pytest.mark.parametrize("awk", AWKS)
@pytest.mark.parametrize(("line", "expected"), EDGE_CASES)
def test_edge_cases(awk, line, expected):
    assert run_awk(awk, line + "\n") == expected + "\n"


@pytest.mark.parametrize("awk", AWKS)
def test_pem_block_is_dropped_whole(awk):
    pem = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nbm90YXJlYWxrZXk=\n"
        "-----END RSA PRIVATE KEY-----\nafter\n"
    )
    assert run_awk(awk, pem) == "before\n[REDACTED]\nafter\n"
    two_keys = "-----BEGIN PRIVATE KEY-----\\nA\\n-----END PRIVATE KEY-----"
    assert "A" not in run_awk(awk, f"{{{two_keys}{two_keys}}}\n").replace(
        "[REDACTED]", ""
    )
    one_line = (
        '{"k": "-----BEGIN PRIVATE KEY-----\\nMIIE\\n-----END PRIVATE KEY-----\\n"}\n'
    )
    assert "MIIE" not in run_awk(awk, one_line)


@pytest.mark.parametrize("awk", AWKS)
def test_url_keeps_scheme_user_and_host(awk):
    out = run_awk(awk, "redis://:hunter2hunter2@cache:6379/0 https://example.com/a\n")
    assert out == "redis://:[REDACTED]@cache:6379/0 https://example.com/a\n"


@pytest.mark.parametrize("awk", AWKS)
def test_without_a_names_file_it_fails_closed(awk, tmp_path):
    result = subprocess.run(
        [awk, "-f", str(REDACT), "-v", f"names={tmp_path / 'missing'}"],
        input=b"POSTGRES_PASSWORD=hunter2hunter2\n",
        capture_output=True,
    )
    assert result.returncode != 0
    assert result.stdout == b""
