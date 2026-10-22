import logging, sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
sys.path.insert(0, ".")
from core.platform import email_service as es
logging.basicConfig(level=logging.INFO)
def S(b): return SimpleNamespace(vigil_email_backend=b, dev_mode=False, smtp_host="relay", smtp_port=2525, smtp_user="", smtp_username="u", smtp_tls=True, smtp_from="vigil@x")
for b, want in [(" SMTP ", True), ("smtp", True), ("console", False), ("sendgrid", False), ("", False)]:
    with patch.object(es, "get_settings", lambda b=b: S(b)), patch.object(es, "get_secret", lambda k: "pw-"+k):
        es._backend = None
        got = es.mail_is_configured(); be = es.get_email_backend()
        assert got == want and isinstance(be, es.SMTPBackend) == want, (b, got, be)
        print(repr(b), "mail_is_configured=", got, type(be).__name__)
        if want:
            smtp = MagicMock(); 
            with patch.object(es.smtplib, "SMTP", return_value=smtp) as ctor:
                es.send_email(to="a@b", subject="s", body="hi")
            ctx = smtp.__enter__.return_value
            msg = ctx.send_message.call_args[0][0]
            print("  SMTP", ctor.call_args, "starttls", ctx.starttls.called, "login", ctx.login.call_args, "From", msg["From"])
            assert ctor.call_args.args == ("relay", 2525) and ctx.starttls.called and ctx.login.call_args.args == ("u", "pw-SMTP_PASSWORD") and msg["From"] == "vigil@x"
print("OK")
