"""Bound connector-built ids to the width of the column they are stored in."""

import hashlib

FINDING_ID_MAX = 50  # findings.finding_id String(50)
EXTERNAL_ID_MAX = 255  # findings.external_id String(255)

_DIGEST_LEN = 16


def fit_id(prefix: str, source_id: str, limit: int) -> str:
    """Return ``prefix + source_id``, unchanged when it fits in ``limit``.

    A longer id keeps its leading characters plus a digest of the whole source
    id, so ids sharing a long prefix (ARNs, ARM resource paths) stay distinct.
    """
    full = f"{prefix}{source_id}"
    if len(full) <= limit:
        return full
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    keep = max(limit - len(prefix) - _DIGEST_LEN - 1, 0)
    return f"{prefix}{source_id[:keep]}-{digest}"[:limit]
