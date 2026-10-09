"""The gateway's own Viewer login and refresh (SP1 §1, decisions 2 and 3)."""

from __future__ import annotations

import json
import os
import time

import pytest

from services.medic_gateway import server, session
from services.medic_gateway.tests.conftest import PASSWORD, get, logins


def refreshes(env) -> int:
    return env.backend.targets().count("/api/auth/refresh")


def test_login_uses_fixed_ua_csrf_pair_and_password_from_file(env):
    assert get(env) == 200
    (lg,) = logins(env)
    h = lg["headers"]
    assert lg["method"] == "POST"
    assert h["User-Agent"] == server.UA == "vigil-medic-gateway/1"
    assert h["Cookie"] == f"csrf_token={h['X-CSRF-Token']}"
    body = json.loads(lg["body"])
    assert body == {"username_or_email": "medic-viewer", "password": PASSWORD}
    assert get(env) == 200 and len(logins(env)) == 1, "the token is reused"
    (read,) = env.backend.data()[:1]
    assert read["headers"]["Authorization"].startswith("Bearer ey")
    assert read["headers"]["User-Agent"] == server.UA


@pytest.mark.parametrize(
    "path",
    [
        b"/api/health",
        b"/api/health/ready",
        b"/api/webhooks/darktrace/health",
        b"/api/webhooks/cloudflare/cloudy/health",
    ],
)
def test_public_paths_need_no_login(env, path):
    env.backend.login_status = 401  # login broken: these must still work
    assert get(env, path) == 200
    assert logins(env) == [] and "Authorization" not in env.backend.seen[0]["headers"]


@pytest.mark.parametrize("ttl,margin", [(1800, 120), (600, 120), (60, 15), (8, 2)])
def test_refresh_margin_is_min_120s_or_a_quarter_of_the_ttl(ttl, margin):
    assert session.refresh_margin(ttl) == margin


def test_refresh_before_expiry(env):
    env.backend.access_ttl = 60
    assert get(env) == 200
    assert get(env) == 200
    assert refreshes(env) == 0, "a 60 s token isn't refreshed on every call"
    env.clock.offset = 60 - 15 + 1  # inside the margin, before expiry
    assert get(env) == 200
    assert refreshes(env) == 1 and len(logins(env)) == 1
    refresh = next(s for s in env.backend.seen if s["target"] == "/api/auth/refresh")
    assert refresh["headers"]["User-Agent"] == server.UA
    assert json.loads(refresh["body"]).keys() == {"refresh_token"}


def test_refresh_once_on_401_then_retry(env):
    assert get(env) == 200
    env.backend.reject_tokens.add(env.session.access)
    env.clock.offset = 60  # an older token, not one minted seconds ago
    assert get(env) == 200
    assert refreshes(env) == 1 and len(logins(env)) == 1
    assert len(env.backend.data()) == 3, "one read, one refused, one retried"


def test_401_twice_is_not_retried_a_third_time(env):
    assert get(env) == 200
    env.clock.offset = 60
    env.backend.reject_all = True
    assert get(env) == 502
    assert len(env.backend.data()) == 3, "first read, refused, one retry; then stop"


def test_failed_refresh_falls_back_to_one_login(env):
    env.backend.access_ttl = 60
    assert get(env) == 200
    env.backend.refresh_status = 401
    env.clock.offset = 50
    assert get(env) == 200
    assert refreshes(env) == 1 and len(logins(env)) == 2


def test_fresh_token_rejected_backs_off_instead_of_relogin(env):
    """Vigil fails closed when Redis is down: login works, every token is 'revoked'."""
    env.backend.reject_all = True
    assert get(env) == 502
    assert env.session.state == "rejected_after_login"
    for _ in range(5):
        assert get(env) == 502
    assert len(logins(env)) == 1
    env.clock.offset = 61  # the back-off is over: one new token, by refresh or login
    get(env)
    assert refreshes(env) + len(logins(env)) == 2


def test_bad_password_stops_after_two_attempts(env):
    env.backend.login_status = 401
    for _ in range(10):
        assert get(env) == 502
        env.clock.offset += 301  # past every timed back-off; never past the stop
    assert len(logins(env)) == 2, "must stay far below Vigil's lockout threshold (5)"
    assert env.session.status()["state"] == "bad_password_stopped"
    env.pw.write_text(PASSWORD + "-rotated")  # a rotated secret earns one new attempt
    os.utime(env.pw, (time.time() + 5, time.time() + 5))
    env.backend.login_status = 200
    assert get(env) == 200
    assert env.session.state == "ok"


def test_bad_password_does_not_retry_in_a_tight_loop(env):
    env.backend.login_status = 401
    for _ in range(20):
        assert get(env) == 502
    assert len(logins(env)) == 1, "the second attempt waits 300 s"
    assert env.session.state == "bad_password"


@pytest.mark.parametrize("code,state", [(423, "locked"), (429, "rate_limited")])
def test_locked_or_rate_limited_honours_retry_after(env, code, state):
    env.backend.login_status = code
    assert get(env) == 502
    assert get(env) == 502
    assert len(logins(env)) == 1
    assert env.session.state == state
    assert env.session.next_login_at - env.clock() > 70  # Retry-After: 77


def test_unexpected_login_status_backs_off(env):
    env.backend.login_status = 500
    for _ in range(5):
        assert get(env) == 502
    assert len(logins(env)) == 1 and env.session.state == "login_error"


def test_unreachable_backend_backs_off_login(env):
    env.backend.srv.shutdown()
    env.backend.srv.server_close()
    for _ in range(3):
        assert get(env) == 502
    assert env.session.state == "backend_unreachable"
    assert env.session.next_login_at - env.clock() > 25


def test_missing_password_file_is_reported_not_raised(env):
    env.pw.unlink()
    assert get(env) == 502
    assert env.session.state == "no_password"
    assert logins(env) == []


def test_status_names_state_and_since_only(env):
    get(env)
    st = env.session.status()
    assert set(st) == {"state", "since"} and st["state"] == "ok"


# ----------------------------------------------------------------------------- review fixes
def test_401_after_a_fresh_token_backs_off_across_polls(env):
    """Review #1: an old token refused, refresh refused, the new login's token refused
    too. That is the revocation store failing closed: back off, report it, and don't
    log in again on every 30 s poll."""
    assert get(env) == 200
    env.backend.reject_all, env.backend.refresh_status = True, 401
    for i in range(10):
        env.clock.offset = 60 + 30 * i
        assert get(env) == 502
        assert env.session.status()["state"] == "rejected_after_login"
    assert len(logins(env)) <= 6, "at most one new login per 60 s back-off"


@pytest.mark.parametrize(
    "body",
    [b"{}", b"not json", b'{"access_token": "x", "refresh_token": "y"}'],
)
def test_unexpected_login_reply_backs_off(env, body):
    """Review #2: a 200 the gateway can't use is a login error, not a retry-now."""
    env.backend.login_body = body
    for _ in range(5):
        assert get(env) == 502
    assert len(logins(env)) == 1 and env.session.state == "login_error"
    assert env.session.access is None


def test_garbage_from_the_backend_backs_off(env):
    env.backend.srv.shutdown()
    env.backend.srv.server_close()
    import socket as _s
    import threading as _t

    srv = _s.socket()
    srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", env.backend.port))
    srv.listen()
    hits = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            hits.append(1)
            c.recv(65536)
            c.sendall(b"garbage\r\n\r\n")
            c.close()

    _t.Thread(target=serve, daemon=True).start()
    try:
        for _ in range(5):
            assert get(env) == 502
        assert len(hits) == 1 and env.session.state == "login_error"
    finally:
        srv.close()


def test_backend_clock_skew_does_not_force_a_login_per_call(env):
    """Review #8: the token's own exp - iat sets its life, not our clock."""
    env.backend.skew = -7200  # the backend is two hours behind: exp is "past"
    for i in range(5):
        env.clock.offset = 120 * i  # well inside the token's 30 minutes
        assert get(env) == 200
    assert len(logins(env)) == 1 and refreshes(env) == 0
