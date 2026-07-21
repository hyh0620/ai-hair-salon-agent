import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import jwt
import pytest

from config.auth_config import AuthConfig
from db.base.session_manager import SessionManager
from services.auth_service import AuthService, AuthServiceError


TEST_SECRET = "test-only-auth-secret-" + ("x" * 64)


def _auth_config(**overrides):
    values = {
        "enabled": True,
        "jwt_secret": TEST_SECRET,
        "jwt_algorithm": "HS256",
        "access_token_minutes": 480,
        "issuer": "ai-hair-salon-agent-test",
        "audience": "ai-hair-salon-web-test",
        "cookie_name": "test_access_token",
        "csrf_cookie_name": "test_csrf_token",
        "cookie_secure": False,
        "cookie_samesite": "lax",
    }
    values.update(overrides)
    return AuthConfig(**values)


def _service(tmp_path, name="auth.db", **config_overrides):
    return AuthService(
        f"sqlite:///{tmp_path / name}",
        config=_auth_config(**config_overrides),
    )


def _register(service, email="User@Example.COM", password="correct horse 123"):
    return service.register_user(
        email=email,
        display_name="  测试   用户  ",
        password=password,
        trace_id="auth-service-test",
    )


def test_registration_normalizes_user_and_stores_only_argon2_hash(tmp_path, caplog):
    db_file = tmp_path / "auth.db"
    service = _service(tmp_path)
    password = "correct horse 123"
    try:
        public_user = _register(service, password=password)
        authenticated = service.authenticate_user(
            email=" user@example.com ",
            password=password,
            trace_id="auth-service-login",
        )
        with pytest.raises(AuthServiceError, match="invalid_credentials"):
            service.authenticate_user(
                email="USER@example.com",
                password="wrong password 123",
                trace_id="auth-service-rejected",
            )
    finally:
        service.close()

    with sqlite3.connect(db_file) as connection:
        row = connection.execute(
            "SELECT id, email, display_name, password_hash, is_active FROM users"
        ).fetchone()

    assert public_user == authenticated
    assert public_user["email"] == "user@example.com"
    assert public_user["display_name"] == "测试 用户"
    assert "password_hash" not in public_user
    assert row[0] == public_user["id"]
    assert row[1:3] == ("user@example.com", "测试 用户")
    assert row[3].startswith("$argon2")
    assert password not in row[3]
    assert row[4] == 1
    assert password not in caplog.text


def test_duplicate_email_is_case_insensitive_and_inactive_login_is_generic(tmp_path):
    db_file = tmp_path / "duplicate.db"
    service = _service(tmp_path, "duplicate.db")
    try:
        user = _register(service, email="Duplicate@Example.com")
        with pytest.raises(AuthServiceError, match="email_already_registered"):
            _register(service, email="duplicate@example.COM")

        with sqlite3.connect(db_file) as connection:
            connection.execute("UPDATE users SET is_active=0 WHERE id=?", (user["id"],))
            connection.commit()

        with pytest.raises(AuthServiceError, match="invalid_credentials"):
            service.authenticate_user(
                email="duplicate@example.com",
                password="correct horse 123",
            )
    finally:
        service.close()


def test_concurrent_duplicate_registration_is_protected_by_database(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'concurrent-users.db'}"
    services = [
        AuthService(db_url, config=_auth_config()),
        AuthService(db_url, config=_auth_config()),
    ]
    barrier = Barrier(2)

    def register(index):
        barrier.wait()
        try:
            services[index].register_user(
                email="Concurrent@Example.com" if index == 0 else "concurrent@example.com",
                display_name=f"用户 {index}",
                password="concurrent password",
            )
            return "success"
        except AuthServiceError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, (0, 1)))
    finally:
        for service in services:
            service.close()

    assert sorted(results) == ["email_already_registered", "success"]
    with sqlite3.connect(tmp_path / "concurrent-users.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_access_token_has_required_claims_and_resolves_active_user(tmp_path):
    service = _service(tmp_path)
    try:
        user = _register(service)
        token, generated_claims = service.create_access_token(user["id"])
        raw_claims = jwt.decode(token, options={"verify_signature": False})
        verified = service.verify_access_token(token)
    finally:
        service.close()

    assert {"sub", "type", "iat", "exp", "iss", "aud", "jti"} <= raw_claims.keys()
    assert raw_claims["sub"] == user["id"]
    assert raw_claims["type"] == "access"
    assert raw_claims["iss"] == generated_claims["iss"]
    assert raw_claims["aud"] == generated_claims["aud"]
    assert verified.user == user


def test_access_token_rejects_expiry_tampering_and_invalid_claims(tmp_path):
    service = _service(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        user = _register(service)
        valid_token, claims = service.create_access_token(user["id"], now=now)
        encoded_claims = jwt.decode(valid_token, options={"verify_signature": False})

        invalid_tokens = []
        expired, _ = service.create_access_token(
            user["id"],
            now=now - timedelta(hours=2),
            expires_delta=timedelta(minutes=1),
        )
        invalid_tokens.append(expired)

        tampered_parts = valid_token.split(".")
        tampered_parts[2] = ("A" if tampered_parts[2][0] != "A" else "B") + tampered_parts[2][1:]
        invalid_tokens.append(".".join(tampered_parts))

        for replacements in (
            {"aud": "wrong-audience"},
            {"iss": "wrong-issuer"},
            {"type": "refresh"},
            {"sub": ""},
            {"sub": "00000000-0000-4000-8000-000000000000"},
        ):
            changed = encoded_claims | replacements
            invalid_tokens.append(jwt.encode(changed, TEST_SECRET, algorithm="HS256"))

        invalid_tokens.append(jwt.encode(encoded_claims, TEST_SECRET, algorithm="HS384"))

        for token in invalid_tokens:
            with pytest.raises(AuthServiceError, match="invalid_token"):
                service.verify_access_token(token)
    finally:
        service.close()


def test_auth_service_rejects_unsafe_or_inconsistent_configuration(tmp_path):
    for config in (
        _auth_config(enabled=False),
        _auth_config(jwt_secret="too-short"),
        _auth_config(jwt_secret=" " * 32),
        _auth_config(jwt_algorithm="HS384"),
        _auth_config(cookie_name="same_cookie", csrf_cookie_name="same_cookie"),
        _auth_config(cookie_samesite="none", cookie_secure=False),
    ):
        service = AuthService(
            f"sqlite:///{tmp_path / f'bad-{config.jwt_algorithm}-{config.enabled}.db'}",
            config=config,
        )
        try:
            with pytest.raises(AuthServiceError, match="auth_not_configured"):
                _register(service)
        finally:
            service.close()


def test_existing_sqlite_database_gains_users_table_idempotently(tmp_path):
    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as connection:
        connection.executescript(
            """
            CREATE TABLE stylists (
                id INTEGER PRIMARY KEY,
                name VARCHAR UNIQUE,
                gender VARCHAR,
                specialties VARCHAR
            );
            INSERT INTO stylists (id, name, gender, specialties)
            VALUES (99, '历史发型师', '女', '女士剪发');
            """
        )

    for _ in range(2):
        manager = SessionManager(f"sqlite:///{db_file}")
        manager.close()

    with sqlite3.connect(db_file) as connection:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        indexes = connection.execute("PRAGMA index_list(users)").fetchall()
        legacy = connection.execute(
            "SELECT id, name FROM stylists WHERE id=99"
        ).fetchone()

    assert {
        "id",
        "email",
        "display_name",
        "password_hash",
        "is_active",
        "created_at",
        "updated_at",
    } <= user_columns
    assert any(row[1] == "ix_users_email" and row[2] == 1 for row in indexes)
    assert legacy == (99, "历史发型师")
