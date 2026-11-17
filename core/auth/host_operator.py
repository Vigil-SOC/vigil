"""Who ran an act from the host, and how they confirmed the person.

Shared by ``scripts/reset_password.py`` and ``core.auth.recover_admin``: the
prompt, and the two halves a trail entry keeps. The typed name and the strings
about the person go beside the hashed payload, where erasure can blank them;
only ids and the compromise choice are hashed.
"""

from __future__ import annotations

import getpass
import os
import socket
import sys
from typing import Optional, TextIO

from core.audit.retention import is_erased
from core.auth.identity import matching_accounts
from core.storage.models import Principal

# The console's IdentityConfirmedBy values, and the words the prompt shows.
METHODS = {
    "in_person": "in person",
    "video_call": "on a video call",
    "phone_call_back": "by calling them back on a number on file",
    "other_verified_channel": "another verified channel",
}


class Operator:
    """Who ran a host act. The name is readable; the rest is erased with it."""

    def __init__(self, name, reason, method, note, behalf, compromise):
        self.name = name
        self.reason = reason
        self.method = method
        self.note = note
        self.behalf = behalf
        self.compromise = compromise

    def beside(self) -> dict:
        personal = {
            "reason": self.reason,
            "identity_confirmed_by": self.method,
            "os_user": getpass.getuser(),
            "host": socket.gethostname(),
        }
        if self.note:
            personal["verification_note"] = self.note
        invoking = os.environ.get("SUDO_USER", "").strip()
        if invoking:
            personal["invoking_user"] = invoking
        return {"operator_text": self.name, "personal": personal}

    def hashed(self) -> dict:
        """Ids and the compromise choice. Strings that name a person stay beside."""
        fields = {}
        if self.behalf:
            fields["on_behalf_of"] = self.behalf
        if self.compromise:
            fields["suspected_compromise"] = True
        return fields


def _ask(prompt: str, stdin: TextIO, stderr: TextIO) -> str:
    print(prompt, end="", file=stderr)
    stderr.flush()
    return stdin.readline().strip()


def behalf(session, who: str, stderr: Optional[TextIO] = None):
    """The operator's principal, '' when they left it blank, None to refuse."""
    stderr = stderr or sys.stderr
    who = who.strip()
    if not who:
        return ""
    matches = matching_accounts(session, who)
    if len(matches) > 1:
        print(
            f"{who!r} matches more than one account. Leave it blank, or name one.",
            file=stderr,
        )
        return None
    user = matches[0] if matches else None
    principal = user and session.get(Principal, user.principal_id)
    if (
        user is None
        or user.service_account is True
        or not user.is_active
        or principal is None
        or principal.deleted_at is not None
        or is_erased(user.username, user.principal_id)
    ):
        print(f"No active account named {who!r}.", file=stderr)
        return None
    return user.principal_id


def ask_operator(
    session,
    compromise: bool,
    stdin: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> Optional[Operator]:
    """Name, reason and verification, before any row changes. None refuses."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr

    def ask(prompt: str) -> str:
        return _ask(prompt, stdin, stderr)

    name = ask("Your name: ")
    if not name:
        print("error: a name is required", file=stderr)
        return None
    reason = ask("Reason: ")
    if not reason:
        print("error: a reason is required", file=stderr)
        return None
    method = ask(
        "How you confirmed them:\n"
        + "".join(f"  {key} — {words}\n" for key, words in METHODS.items())
        + "Verification method: "
    )
    if not method:
        print("error: a verification method is required", file=stderr)
        return None
    if method not in METHODS:
        print(
            "error: verification method must be one of " + ", ".join(METHODS),
            file=stderr,
        )
        return None
    note = ""
    if method == "other_verified_channel":
        note = ask("Note for that channel: ")
        if not note:
            print("error: that verification method needs a note", file=stderr)
            return None
    who = behalf(
        session, ask("Your Vigil account (username or email), or leave blank: "), stderr
    )
    if who is None:
        return None
    return Operator(name, reason, method, note, who or None, compromise)
