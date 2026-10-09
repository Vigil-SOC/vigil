"""K2 redaction choke point (minimal, S3): pattern-only, fail closed.

Everything a sensor reads passes `redact_observation` before any sink sees it, and
every log record Medic writes passes `install_log_redaction`'s record factory.
"""

from services.medic.redact.choke import (
    LogRedaction,
    install_log_redaction,
    redact_observation,
)
from services.medic.redact.rules import (
    REDACTED,
    REDACTION_VERSION,
    Redactor,
    secret_key,
)

__all__ = [
    "REDACTED",
    "REDACTION_VERSION",
    "LogRedaction",
    "Redactor",
    "install_log_redaction",
    "redact_observation",
    "secret_key",
]
