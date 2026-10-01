"""The last JWT_SECRET_KEY assignment in a .env is the one restore reads."""

from core.backup.restore import _env_jwt


def test_env_jwt_uses_the_last_assignment(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        'JWT_SECRET_KEY="first"\nOTHER=1\nJWT_SECRET_KEY=second\n',
        encoding="utf-8",
    )
    assert _env_jwt(path) == "second"

    path.write_text("JWT_SECRET_KEY=first\nJWT_SECRET_KEY=\n", encoding="utf-8")
    assert _env_jwt(path) is None
    assert _env_jwt(tmp_path / "missing") is None
