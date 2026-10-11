"""K2 redaction choke point (minimal, S3): pattern-only, fail closed.

Everything a sensor reads passes `redact_observation` before any sink sees it.
`install_log_redaction` installs a record factory that redacts every log record;
Medic's entry points don't install it yet.
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
