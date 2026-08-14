"""
Pluggable email delivery.

Two backends ship out of the box:
- **ConsoleBackend** — logs the message to the application log. The dev
  default; useful for seeing the password-reset link without configuring
  SMTP. Also useful in CI where tests need the outbound message without
  a live SMTP server.
- **SMTPBackend** — standard smtplib. Point it at SES / SendGrid /
  Mailgun / corporate SMTP relay via env vars — we deliberately don't
  depend on any single provider's SDK.

Select via `VIGIL_EMAIL_BACKEND=console|smtp`. Default: `console`, so a
misconfigured production deploy won't crash auth; it'll just not send the
email and log the content instead. Flip to `smtp` when SMTP creds are set.
"""

import logging
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Optional

from core.config import get_settings
from core.secrets import get_secret

logger = logging.getLogger(__name__)


class EmailBackend(ABC):
    @abstractmethod
    def send(
        self, *, to: str, subject: str, body: str, from_addr: Optional[str] = None
    ) -> None: ...


class ConsoleBackend(EmailBackend):
    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: Optional[str] = None,
    ) -> None:
        logger.info(
            "[email/console] to=%s from=%s subject=%r\n%s",
            to,
            from_addr or get_settings().smtp_from,
            subject,
            body,
        )


class SMTPBackend(EmailBackend):
    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: Optional[bool] = None,
        default_from: Optional[str] = None,
    ):
        settings = get_settings()
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.username = username or settings.smtp_user or settings.smtp_username
        self.password = password or get_secret("SMTP_PASSWORD")
        self.use_tls = use_tls if use_tls is not None else settings.smtp_tls
        self.default_from = default_from or settings.smtp_from

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        from_addr: Optional[str] = None,
    ) -> None:
        if not self.host:
            raise RuntimeError(
                "SMTPBackend selected but SMTP_HOST is not set. "
                "Configure SMTP_HOST / SMTP_PORT / credentials, or set "
                "VIGIL_EMAIL_BACKEND=console."
            )

        msg = EmailMessage()
        msg["From"] = from_addr or self.default_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)


_backend: Optional[EmailBackend] = None


def get_email_backend() -> EmailBackend:
    """Resolve the configured email backend. Lazy so env is picked up at use."""
    global _backend
    if _backend is not None:
        return _backend
    choice = get_settings().vigil_email_backend.strip().lower()
    if choice == "smtp":
        _backend = SMTPBackend()
    else:
        if choice != "console":
            logger.warning(
                "Unknown VIGIL_EMAIL_BACKEND=%r; falling back to console backend",
                choice,
            )
        _backend = ConsoleBackend()
    return _backend


def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    from_addr: Optional[str] = None,
) -> None:
    """Convenience wrapper — logs and swallows exceptions so an email
    outage does not turn into a user-facing 500. Callers that need the
    error (e.g. admin ops) can call the backend directly."""
    try:
        get_email_backend().send(to=to, subject=subject, body=body, from_addr=from_addr)
    except Exception as exc:
        logger.error("Email send failed: to=%s subject=%r error=%s", to, subject, exc)
