import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import jwt
import pytest

from config.auth_config import AuthConfig
from config.time_config import utc_now_naive
from db.base.session_manager import SessionManager
from services.auth_service import AuthService, AuthServiceError


TEST_SECRET = "auth-session-test-secret-" + ("s" * 64)
PASSWORD = "account password 123"


class FakeClock:
    def __init__(self, value=None):
        self.value = value or utc_now_naive()

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def _config(**overrides):
    values = {
        "enabled": True,
        "jwt_secret": TEST_SECRET,
        "jwt_algorithm": "HS256",
        "access_token_minutes": 15,
        "issuer": "auth-session-test",
        "audience": "auth-session-web-test",
        "cookie_name": "test_access_token",
        "csrf_cookie_name": "test_csrf_token",
        "cookie_secure": False,
        "cookie_samesite": "lax",
        "refresh_token_days": 30,
        "refresh_cookie_name": "test_refresh_token",
        "refresh_cookie_path": "/api/auth",
        "refresh_reuse_grace_seconds": 3,
        "auth_session_retention_days": 30,
    }
    values.update(overrides)
    return AuthConfig(**values)


def _service(tmp_path, name="sessions.db", *, clock=None, **config_overrides):
    return AuthService(
        f"sqlite:///{tmp_path / name}",
        config=_config(**config_overrides),
        clock=clock or FakeClock(),
    )


def _register(service, email="member@example.com"):
    return service.register_with_session(
        email=email,
        display_name="测试会员",
        password=PASSWORD,
        trace_id="auth-session-test",
    )


def test_auth_config_defaults_and_cookie_boundaries(monkeypatch):
    for name in (
        "AUTH_ACCESS_TOKEN_MINUTES",
        "AUTH_REFRESH_TOKEN_DAYS",
        "AUTH_REFRESH_COOKIE_NAME",
        "AUTH_REFRESH_COOKIE_PATH",
        "AUTH_REFRESH_REUSE_GRACE_SECONDS",
        "AUTH_AUTH_SESSION_RETENTION_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_SECRET)

    config = AuthConfig.from_env()

    assert config.access_token_minutes == 15
    assert config.refresh_token_days == 30
    assert config.refresh_cookie_name == "salon_refresh_token"
    assert config.refresh_cookie_path == "/api/auth"
    assert config.refresh_reuse_grace_seconds == 3
    assert config.auth_session_retention_days == 30
    assert config.is_configured


@pytest.mark.parametrize(
    "overrides",
    [
        {"refresh_cookie_name": "test_access_token"},
        {"refresh_cookie_name": "test_csrf_token"},
        {"refresh_cookie_path": "api/auth"},
        {"cookie_samesite": "none", "cookie_secure": False},
        {"refresh_token_days": 0},
        {"refresh_reuse_grace_seconds": 0},
        {"auth_session_retention_days": 0},
    ],
)
def test_auth_config_rejects_inconsistent_session_settings(overrides):
    assert not _config(**overrides).is_configured


def test_registration_persists_only_refresh_hash_and_links_session(tmp_path):
    db_file = tmp_path / "persisted.db"
    service = _service(tmp_path, "persisted.db")
    try:
        issued = _register(service)
    finally:
        service.close()

    with sqlite3.connect(db_file) as connection:
        session_row = connection.execute(
            "SELECT id, user_id, revoked_at FROM auth_sessions"
        ).fetchone()
        refresh_row = connection.execute(
            "SELECT session_id, token_hash, used_at, replaced_by_token_id "
            "FROM auth_refresh_tokens"
        ).fetchone()

    assert session_row == (issued.auth_session_id, issued.user["id"], None)
    assert refresh_row[0] == issued.auth_session_id
    assert refresh_row[1] == hashlib.sha256(
        issued.refresh_token.encode("utf-8")
    ).hexdigest()
    assert len(refresh_row[1]) == 64
    assert issued.refresh_token != refresh_row[1]
    assert refresh_row[2:] == (None, None)


def test_auth_session_foreign_keys_cascade_without_orphans(tmp_path):
    db_file = tmp_path / "cascade.db"
    service = _service(tmp_path, "cascade.db")
    try:
        issued = _register(service)
    finally:
        service.close()

    with sqlite3.connect(db_file) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM users WHERE id=?", (issued.user["id"],))
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_refresh_tokens").fetchone()[0] == 0


def test_existing_database_gains_auth_tables_idempotently(tmp_path):
    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as connection:
        connection.executescript(
            """
            CREATE TABLE stylists (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE);
            INSERT INTO stylists (id, name) VALUES (42, '历史老师');
            """
        )

    for _ in range(2):
        manager = SessionManager(f"sqlite:///{db_file}")
        manager.close()

    with sqlite3.connect(db_file) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        legacy = connection.execute(
            "SELECT id, name FROM stylists WHERE id=42"
        ).fetchone()
    assert {"auth_sessions", "auth_refresh_tokens"} <= tables
    assert legacy == (42, "历史老师")


def test_access_jwt_requires_valid_persisted_sid_and_preserves_owner_subject(tmp_path):
    service = _service(tmp_path)
    try:
        issued = _register(service)
        claims = jwt.decode(issued.access_token, options={"verify_signature": False})
        verified = service.verify_access_token(issued.access_token)
    finally:
        service.close()

    assert claims["sub"] == issued.user["id"]
    assert claims["sid"] == issued.auth_session_id
    assert verified.auth_session_id == issued.auth_session_id
    assert verified.user["id"] == issued.user["id"]


@pytest.mark.parametrize("sid_value", [None, "not-a-uuid", "00000000-0000-4000-8000-000000000000"])
def test_access_jwt_rejects_missing_malformed_or_unknown_sid(tmp_path, sid_value):
    service = _service(tmp_path)
    try:
        issued = _register(service)
        claims = jwt.decode(issued.access_token, options={"verify_signature": False})
        if sid_value is None:
            claims.pop("sid")
        else:
            claims["sid"] = sid_value
        token = jwt.encode(claims, TEST_SECRET, algorithm="HS256")
        with pytest.raises(AuthServiceError, match="invalid_token"):
            service.verify_access_token(token)
    finally:
        service.close()


def test_access_jwt_rejects_sid_owned_by_another_user(tmp_path):
    service = _service(tmp_path)
    try:
        first = _register(service, "first@example.com")
        second = _register(service, "second@example.com")
        claims = jwt.decode(first.access_token, options={"verify_signature": False})
        claims["sid"] = second.auth_session_id
        token = jwt.encode(claims, TEST_SECRET, algorithm="HS256")
        with pytest.raises(AuthServiceError, match="invalid_token"):
            service.verify_access_token(token)
    finally:
        service.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP",
        "UPDATE auth_sessions SET expires_at='2000-01-01 00:00:00'",
        "UPDATE users SET is_active=0",
    ],
)
def test_access_jwt_rejects_revoked_expired_or_inactive_context(tmp_path, statement):
    db_file = tmp_path / "invalid-context.db"
    service = _service(tmp_path, "invalid-context.db")
    try:
        issued = _register(service)
        with sqlite3.connect(db_file) as connection:
            connection.execute(statement)
            connection.commit()
        with pytest.raises(AuthServiceError, match="invalid_token"):
            service.verify_access_token(issued.access_token)
    finally:
        service.close()


def test_refresh_rotation_is_one_time_and_preserves_absolute_session_expiry(tmp_path):
    clock = FakeClock()
    service = _service(tmp_path, clock=clock)
    try:
        issued = _register(service)
        clock.advance(minutes=5)
        rotated = service.refresh_session(issued.refresh_token, now=clock())
        duplicate = service.refresh_session(issued.refresh_token, now=clock())
    finally:
        service.close()

    assert rotated.status == "success"
    assert rotated.auth_session_id == issued.auth_session_id
    assert rotated.session_expires_at == issued.session_expires_at
    assert rotated.refresh_token != issued.refresh_token
    assert duplicate.status == "concurrent"


def test_two_connections_can_rotate_same_refresh_token_only_once(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'concurrent-refresh.db'}"
    clock = FakeClock()
    creator = AuthService(db_url, config=_config(), clock=clock)
    try:
        issued = _register(creator)
    finally:
        creator.close()
    services = [
        AuthService(db_url, config=_config(), clock=clock),
        AuthService(db_url, config=_config(), clock=clock),
    ]
    barrier = Barrier(2)

    def rotate(index):
        barrier.wait()
        return services[index].refresh_session(issued.refresh_token, now=clock()).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(rotate, (0, 1)))
    finally:
        for service in services:
            service.close()

    assert sorted(results) == ["concurrent", "success"]


def test_replay_after_grace_revokes_session_and_latest_credentials(tmp_path):
    clock = FakeClock()
    service = _service(tmp_path, clock=clock)
    try:
        issued = _register(service)
        rotated = service.refresh_session(issued.refresh_token, now=clock())
        clock.advance(seconds=4)
        replay = service.refresh_session(issued.refresh_token, now=clock())
        latest_refresh = service.refresh_session(rotated.refresh_token, now=clock())
        with pytest.raises(AuthServiceError, match="invalid_token"):
            service.verify_access_token(rotated.access_token)
    finally:
        service.close()

    assert replay.status == "replay"
    assert latest_refresh.status == "invalid"


@pytest.mark.parametrize("failure", ["token", "jwt"])
def test_rotation_generation_failures_do_not_consume_old_token(tmp_path, failure, monkeypatch):
    service = _service(tmp_path)
    try:
        issued = _register(service)
        if failure == "token":
            monkeypatch.setattr(
                service.auth_sessions,
                "_token_factory",
                lambda _size: (_ for _ in ()).throw(RuntimeError("generation failed")),
            )
        else:
            monkeypatch.setattr(
                service,
                "_issue_access_for_session",
                lambda *_args: (_ for _ in ()).throw(RuntimeError("signing failed")),
            )
        with pytest.raises(AuthServiceError):
            service.refresh_session(issued.refresh_token)
    finally:
        service.close()

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        used_at = connection.execute(
            "SELECT used_at FROM auth_refresh_tokens WHERE token_hash=?",
            (hashlib.sha256(issued.refresh_token.encode()).hexdigest(),),
        ).fetchone()[0]
    assert used_at is None


def test_registration_session_failure_rolls_back_new_user(tmp_path, monkeypatch):
    service = _service(tmp_path, "register-rollback.db")
    monkeypatch.setattr(
        service.auth_session_repo,
        "add_refresh_token_in_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )
    try:
        with pytest.raises(AuthServiceError, match="persistence_error"):
            _register(service)
    finally:
        service.close()

    with sqlite3.connect(tmp_path / "register-rollback.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_refresh_tokens").fetchone()[0] == 0


def test_registration_credential_generation_failure_creates_no_rows(tmp_path, monkeypatch):
    service = _service(tmp_path, "register-credentials.db")
    monkeypatch.setattr(
        service.auth_sessions,
        "_token_factory",
        lambda _size: (_ for _ in ()).throw(RuntimeError("generation failed")),
    )
    try:
        with pytest.raises(AuthServiceError, match="credential_issue_error"):
            _register(service)
    finally:
        service.close()

    with sqlite3.connect(tmp_path / "register-credentials.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0


def test_bounded_lazy_cleanup_removes_only_old_inactive_session_history(tmp_path):
    db_file = tmp_path / "cleanup.db"
    clock = FakeClock()
    service = _service(tmp_path, "cleanup.db", clock=clock)
    try:
        old = _register(service, "old@example.com")
        old_time = clock() - timedelta(days=31)
        old_time_sql = old_time.strftime("%Y-%m-%d %H:%M:%S.%f")
        with sqlite3.connect(db_file) as connection:
            connection.execute(
                "UPDATE auth_sessions SET expires_at=?, revoked_at=? WHERE id=?",
                (old_time_sql, old_time_sql, old.auth_session_id),
            )
            connection.execute(
                "UPDATE auth_refresh_tokens SET expires_at=?, revoked_at=? WHERE session_id=?",
                (old_time_sql, old_time_sql, old.auth_session_id),
            )
            connection.commit()
        active = _register(service, "active@example.com")
    finally:
        service.close()

    with sqlite3.connect(db_file) as connection:
        session_ids = {
            row[0] for row in connection.execute("SELECT id FROM auth_sessions")
        }
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert old.auth_session_id not in session_ids
    assert active.auth_session_id in session_ids
    assert user_count == 2


def test_logout_revokes_only_selected_session(tmp_path):
    service = _service(tmp_path)
    try:
        first = _register(service)
        second = service.authenticate_with_session(
            email=first.user["email"],
            password=PASSWORD,
        )
        assert service.revoke_session(
            auth_session_id=first.auth_session_id,
            user_id=first.user["id"],
        )
        with pytest.raises(AuthServiceError, match="invalid_token"):
            service.verify_access_token(first.access_token)
        assert service.verify_access_token(second.access_token).user["id"] == first.user["id"]
        assert service.refresh_session(first.refresh_token).status == "invalid"
        assert service.refresh_session(second.refresh_token).status == "success"
    finally:
        service.close()
