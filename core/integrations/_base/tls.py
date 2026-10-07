"""Per-integration TLS trust: a private CA scoped to one integration's clients.

``SSL_CERT_FILE`` trusts a CA for the whole process, so an installer-generated
root (a Wazuh indexer ships one) could vouch for every host Vigil calls. A
``ca_cert_path`` on the integration instead builds a context for that
integration's own clients only; every other client keeps the default store.
"""

from __future__ import annotations

import ssl
from typing import Optional, Union


def tls_verify(
    verify_ssl: bool, ca_cert_path: Optional[str] = None
) -> Union[bool, ssl.SSLContext]:
    """The ``verify=`` value for an integration's httpx client.

    ``verify_ssl=False`` wins over a path. A blank path means the default
    store, which ``SSL_CERT_FILE`` still controls. A path replaces the store
    for this client, trusting only the CAs in that PEM. Call it where the
    client is built, not at service construction, so an unusable file fails
    the request that needs it rather than reading as "not configured".
    """
    if not verify_ssl:
        return False
    if not ca_cert_path:
        return True
    try:
        return ssl.create_default_context(cafile=ca_cert_path)
    except OSError as exc:  # ssl.SSLError is an OSError too
        # ssl reports "No such file or directory" without the path.
        raise OSError(f"CA certificate {ca_cert_path!r} not usable: {exc}") from exc
