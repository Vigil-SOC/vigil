"""Per-integration CA trust: ``tls_verify`` and the clients that call it."""

from __future__ import annotations

import datetime
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from core.integrations._base.tls import tls_verify
from core.integrations.elastic.client import ElasticService
from core.integrations.splunk.client import SplunkService
from core.integrations.vstrike.client import VStrikeService


@pytest.fixture
def ca_pem(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "root-ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_verify_off_wins_over_a_path(ca_pem):
    assert tls_verify(False, ca_pem) is False


@pytest.mark.parametrize("path", [None, ""])
def test_blank_path_keeps_the_default_store(path):
    assert tls_verify(True, path) is True


def test_path_trusts_only_that_ca(ca_pem):
    ctx = tls_verify(True, ca_pem)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert [c["subject"] for c in ctx.get_ca_certs()] == [
        ((("commonName", "Test Root CA"),),)
    ]


def test_missing_or_junk_file_names_the_path(tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate\n")
    for path in (tmp_path / "no-such-ca.pem", junk):
        with pytest.raises(OSError, match=path.name):
            tls_verify(True, str(path))


# A bad path must not raise at construction ("not configured"); it fails the
# integration's own connection check, naming the file.


def test_splunk_bad_path_fails_the_connection_not_the_constructor(tmp_path):
    svc = SplunkService(
        "https://splunk.test:8089",
        "u",
        "p",
        verify_ssl=True,
        ca_cert_path=str(tmp_path / "no-such-ca.pem"),
    )
    ok, msg = svc.test_connection()
    assert ok is False and "no-such-ca.pem" in msg
    with pytest.raises(OSError, match="no-such-ca.pem"):
        svc.search("index=main")


def test_vstrike_bad_path_fails_the_connection_not_the_constructor(tmp_path):
    svc = VStrikeService(
        "https://vstrike.test",
        username="u",
        password="p",
        ca_cert_path=str(tmp_path / "no-such-ca.pem"),
    )
    ok, msg = svc.test_connection()
    assert ok is False and "no-such-ca.pem" in msg


@pytest.mark.asyncio
async def test_elastic_bad_path_fails_the_request_not_the_constructor(tmp_path):
    svc = ElasticService(
        "https://es.test:9200", ca_cert_path=str(tmp_path / "no-such-ca.pem")
    )
    ok, msg = await svc.test_connection()
    assert ok is False and "no-such-ca.pem" in msg
    with pytest.raises(OSError, match="no-such-ca.pem"):
        await svc.search({"match_all": {}})
