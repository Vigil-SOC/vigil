"""The one parser for a source anomaly score.

Storage and ingestion both read scores. The function lives here so the
storage tier does not import the ingestion domain to get it.
"""

import math
from typing import Any, Optional


def optional_float(value: Any) -> Optional[float]:
    """Parse a source score. Missing or unusable values are no score.

    Real numbers and numeric strings parse as before, including surrounding
    whitespace and booleans (``True`` → 1.0). NaN, ±infinity, overflow, and
    anything ``float()`` cannot read are None. The value is not clamped.
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed
