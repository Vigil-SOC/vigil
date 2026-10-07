"""Validation for caller-supplied Elasticsearch/OpenSearch search parameters.

An index name is interpolated into the request path and a time range into date
math, so both are checked before they reach either.
"""

import re

# Index names, wildcards, comma lists (including ``-`` exclusions) and
# cross-cluster ``remote:index`` targets. No path, query or fragment characters.
_INDEX_ENTRY = re.compile(r"^[A-Za-z0-9._*:-]+$")
_TIME_RANGE = re.compile(r"^[0-9]{1,6}[smhdwMy]$")


def validate_index(index: str) -> str:
    """Return ``index`` unchanged if it names only indices, else raise ValueError."""
    if not isinstance(index, str) or not index:
        raise ValueError("index must be a non-empty string")
    for entry in index.split(","):
        # Names starting with "_" are API endpoints; "." and ".." are path segments.
        if (
            not _INDEX_ENTRY.match(entry)
            or entry.startswith("_")
            or entry.lstrip("-").startswith("_")
            or ".." in entry
            or entry in (".", "-.")
        ):
            raise ValueError(f"Invalid index name: {index!r}")
    return index


def validate_time_range(time_range: str) -> str:
    """Return ``time_range`` if it is a relative duration such as ``24h``."""
    if not isinstance(time_range, str) or not _TIME_RANGE.match(time_range):
        raise ValueError(
            f"Invalid time_range: {time_range!r}; use a number and a unit, e.g. '24h', '7d'"
        )
    return time_range
