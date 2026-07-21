import hashlib
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import jwt
from fastapi.testclient import TestClient

import api.auth as auth_api
from app import create_app
from config.auth_rate_limit_config import AuthRateLimitConfig
from config.time_config import time_config
from services.auth_rate_limit_service import AuthRateLimiter
from services.auth_service import AuthService


TEST_SECRET = "refresh-api-test-secret-" + ("r" * 64)
PASSWORD = "account password 123"


def _configure(monkeypatch, tmp_path, name="refresh-api.db"):
    db_file = tmp_path / name
    values = {
        "DATABASE_URL": f"sqlite:///{db_file}",
        "RAG_MCP_ENABLED": "false",
        "WEATHER_ENABLED": "false",
        "AUTH_ENABLED": "true",
        "AUTH_JWT_SECRET": TEST_SECRET,
        "AUTH_JWT_ALGORITHM": "HS256",
        "AUTH_ACCESS_TOKEN_MINUTES": "15",
        "AUTH_REFRESH_TOKEN_DAYS": "30",
        "AUTH_JWT_ISSUER": "refresh-api-test",
        "AUTH_JWT_AUDIENCE": "refresh-api-web-test",
        "AUTH_COOKIE_NAME": "salon_access_token",
        "AUTH_REFRESH_COOKIE_NAME": "salon_refresh_token",
        "AUTH_REFRESH_COOKIE_PATH": "/api/auth",
        "AUTH_CSRF_COOKIE_NAME": "salon_csrf_token",
        "AUTH_COOKIE_SECURE": "false",
        "AUTH_COOKIE_SAMESITE": "lax",
        "AUTH_REFRESH_REUSE_GRACE_SECONDS": "3",
        "AUTH_AUTH_SESSION_RETENTION_DAYS": "30",
        "AUTH_REFRESH_CLIENT_LIMIT": "100",
        "AUTH_REFRESH_CLIENT_WINDOW_SECONDS": "60",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return db_file


def _register(client, email="member@example.com", chat_session="guest-chat"):
    response = client.post(
        "/api/auth/register",
        headers={"X-Chat-Session-ID": chat_session},
        json={
            "email": email,
            "display_name": "测试会员",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return response


def _csrf(client):
    return client.cookies.get("salon_csrf_token")


def _set_cookie(client, name, value, path="/"):
    try:
        client.cookies.delete(name, domain="testserver.local", path=path)
    except KeyError:
        pass
    client.cookies.set(name, value, domain="testserver.local", path=path)


def test_register_sets_three_cookie_contracts_without_exposing_refresh(monkeypatch, tmp_path):
    db_file = _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        response = _register(client)

    body = response.json()
    cookies = response.headers.get_list("set-cookie")
    refresh_cookie = next(item for item in cookies if item.startswith("salon_refresh_token="))
    access_cookie = next(item for item in cookies if item.startswith("salon_access_token="))
    csrf_cookie = next(item for item in cookies if item.startswith("salon_csrf_token="))
    assert body["data"]["access_token"]
    assert "refresh_token" not in body["data"]
    assert "sid" not in body["data"]
    assert "auth_session_id" not in body["data"]
    assert "HttpOnly" in access_cookie
    assert "Path=/" in access_cookie
    assert "HttpOnly" in refresh_cookie
    assert "Path=/api/auth" in refresh_cookie
    assert "HttpOnly" not in csrf_cookie

    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1
        token_hash = connection.execute(
            "SELECT token_hash FROM auth_refresh_tokens"
        ).fetchone()[0]
    assert len(token_hash) == 64


def test_refresh_requires_cookie_and_double_submit_csrf(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        missing_cookie = client.post("/api/auth/refresh")
        _register(client)
        missing_csrf = client.post("/api/auth/refresh")
        wrong_csrf = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": "wrong"},
        )
        success = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": _csrf(client)},
        )

    assert missing_cookie.status_code == 401
    assert missing_cookie.json() == {"detail": "登录状态已失效，请重新登录"}
    assert missing_csrf.status_code == wrong_csrf.status_code == 403
    assert success.status_code == 200


def test_refresh_rotates_access_refresh_and_csrf_without_chat_session(monkeypatch, tmp_path):
    db_file = _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client, chat_session="chat-before")
        old_access = registered.json()["data"]["access_token"]
        old_refresh = client.cookies.get("salon_refresh_token")
        old_csrf = _csrf(client)
        old_sid = jwt.decode(old_access, options={"verify_signature": False})["sid"]

        refreshed = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": old_csrf, "X-Chat-Session-ID": "chat-before"},
        )
        new_access = refreshed.json()["data"]["access_token"]
        new_refresh = client.cookies.get("salon_refresh_token")
        new_csrf = _csrf(client)

    assert refreshed.status_code == 200
    assert "session_id" not in refreshed.json()["data"]
    assert "refresh_token" not in refreshed.json()["data"]
    assert new_access != old_access
    assert new_refresh != old_refresh
    assert new_csrf != old_csrf
    assert jwt.decode(new_access, options={"verify_signature": False})["sid"] == old_sid
    with sqlite3.connect(db_file) as connection:
        old_row = connection.execute(
            "SELECT used_at, replaced_by_token_id FROM auth_refresh_tokens "
            "WHERE token_hash=?",
            (hashlib.sha256(old_refresh.encode()).hexdigest(),),
        ).fetchone()
        new_row = connection.execute(
            "SELECT id FROM auth_refresh_tokens WHERE token_hash=?",
            (hashlib.sha256(new_refresh.encode()).hexdigest(),),
        ).fetchone()
    assert old_row[0] is not None
    assert old_row[1] == new_row[0]


def test_refresh_succeeds_without_valid_access_cookie(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client)
        claims = jwt.decode(
            registered.json()["data"]["access_token"],
            options={"verify_signature": False},
        )
        service = AuthService()
        try:
            expired, _ = service.create_access_token(
                claims["sub"],
                claims["sid"],
                now=time_config.now() - timedelta(hours=2),
                expires_delta=timedelta(minutes=1),
            )
        finally:
            service.close()
        _set_cookie(client, "salon_access_token", expired)
        response = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": _csrf(client)},
        )
        me = client.get("/api/auth/me")

    assert response.status_code == 200
    assert me.status_code == 200


def test_concurrent_refresh_requests_return_one_success_and_one_409(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as creator:
        _register(creator)
        raw_refresh = creator.cookies.get("salon_refresh_token")
        csrf = _csrf(creator)
    barrier = Barrier(2)

    def rotate(_index):
        with TestClient(app) as client:
            _set_cookie(client, "salon_refresh_token", raw_refresh, "/api/auth")
            _set_cookie(client, "salon_csrf_token", csrf)
            barrier.wait()
            response = client.post(
                "/api/auth/refresh",
                headers={"X-CSRF-Token": csrf},
            )
            return response.status_code, response.headers.get("Retry-After")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(rotate, (0, 1)))

    assert sorted(code for code, _header in results) == [200, 409]
    conflict = next(item for item in results if item[0] == 409)
    assert conflict[1] == "1"


def test_invalid_refresh_clears_all_auth_cookies(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _set_cookie(client, "salon_refresh_token", "x" * 64, "/api/auth")
        _set_cookie(client, "salon_access_token", "invalid")
        _set_cookie(client, "salon_csrf_token", "csrf")
        response = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": "csrf"},
        )

    cookies = response.headers.get_list("set-cookie")
    assert response.status_code == 401
    assert response.json() == {"detail": "登录状态已失效，请重新登录"}
    assert any(item.startswith("salon_access_token=") and "Max-Age=0" in item for item in cookies)
    assert any(
        item.startswith("salon_refresh_token=")
        and "Path=/api/auth" in item
        and "Max-Age=0" in item
        for item in cookies
    )
    assert any(item.startswith("salon_csrf_token=") and "Max-Age=0" in item for item in cookies)


def test_cookie_logout_revokes_session_and_copied_credentials(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as client:
        registered = _register(client, chat_session="chat-before")
        access = registered.json()["data"]["access_token"]
        refresh = client.cookies.get("salon_refresh_token")
        csrf = _csrf(client)
        missing_csrf = client.post("/api/auth/logout")
        logged_out = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf, "X-Chat-Session-ID": "chat-before"},
        )
        repeated = client.post("/api/auth/logout")

    with TestClient(app) as copied:
        access_after = copied.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access}"},
        )
        _set_cookie(copied, "salon_refresh_token", refresh, "/api/auth")
        _set_cookie(copied, "salon_csrf_token", "copied-csrf")
        refresh_after = copied.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": "copied-csrf"},
        )

    assert missing_csrf.status_code == 403
    assert logged_out.status_code == repeated.status_code == 200
    assert logged_out.json()["data"]["session_id"] != "chat-before"
    assert access_after.status_code == refresh_after.status_code == 401


def test_bearer_logout_needs_no_csrf_and_only_revokes_bearer_session(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as first:
        first_registration = _register(first)
        first_token = first_registration.json()["data"]["access_token"]
    with TestClient(app) as second:
        login = second.post(
            "/api/auth/login",
            json={"email": "member@example.com", "password": PASSWORD},
        )
        second_token = login.json()["data"]["access_token"]

    with TestClient(app) as bearer:
        logout = bearer.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        first_after = bearer.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        second_after = bearer.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {second_token}"},
        )

    assert logout.status_code == 200
    assert first_after.status_code == 401
    assert second_after.status_code == 200


def test_logout_uses_refresh_when_access_cookie_is_expired(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as client:
        registered = _register(client)
        access = registered.json()["data"]["access_token"]
        claims = jwt.decode(access, options={"verify_signature": False})
        service = AuthService()
        try:
            expired, _ = service.create_access_token(
                claims["sub"],
                claims["sid"],
                now=time_config.now() - timedelta(hours=2),
                expires_delta=timedelta(minutes=1),
            )
        finally:
            service.close()
        _set_cookie(client, "salon_access_token", expired)
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": _csrf(client)},
        )

    with TestClient(app) as copied:
        after = copied.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {access}"},
        )
    assert logout.status_code == 200
    assert after.status_code == 401


def test_refresh_rate_limit_runs_before_auth_service_and_ignores_proxy_headers(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    app = create_app()
    config = AuthRateLimitConfig(refresh_client_limit=1, refresh_client_window_seconds=60)
    app.state.auth_rate_limiter = AuthRateLimiter(config)
    app.state.auth_rate_limiter.consume(
        "refresh_client",
        auth_api.client_fingerprint("192.0.2.80"),
        1,
        60,
    )

    class ForbiddenAuthService:
        def __init__(self, *args, **kwargs):
            raise AssertionError("rate-limited refresh reached AuthService")

    monkeypatch.setattr(auth_api, "AuthService", ForbiddenAuthService)
    with TestClient(app, client=("192.0.2.80", 50000)) as client:
        response = client.post(
            "/api/auth/refresh",
            headers={
                "X-Forwarded-For": "203.0.113.10",
                "X-Real-IP": "203.0.113.11",
            },
        )
    assert response.status_code == 429
    assert response.json() == {"detail": "请求过于频繁，请稍后再试"}
    assert response.headers["Retry-After"] == "60"


def test_refresh_logs_and_responses_do_not_expose_credential_material(monkeypatch, tmp_path, caplog):
    _configure(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)
    with TestClient(create_app()) as client:
        _register(client)
        raw_refresh = client.cookies.get("salon_refresh_token")
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        response = client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": _csrf(client)},
        )

    assert response.status_code == 200
    assert raw_refresh not in caplog.text
    assert token_hash not in caplog.text
    assert raw_refresh not in response.text
    assert token_hash not in response.text


def test_refresh_openapi_has_no_token_body_and_typed_responses(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        operation = client.get("/openapi.json").json()["paths"]["/api/auth/refresh"]["post"]

    assert "requestBody" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AuthRefreshResponse"
    )
    assert operation["responses"]["401"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AuthRefreshInvalidResponse"
    )
    assert operation["responses"]["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AuthRefreshConflictResponse"
    )
