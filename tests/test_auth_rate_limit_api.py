import logging
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import api.auth as auth_api
from app import create_app
from config.auth_rate_limit_config import AuthRateLimitConfig
from services.auth_rate_limit_service import (
    LOGIN_CLIENT_ACCOUNT_SCOPE,
    LOGIN_CLIENT_SCOPE,
    REGISTER_CLIENT_SCOPE,
    AuthRateLimiter,
    account_fingerprint,
    client_fingerprint,
    login_pair_fingerprint,
)


TEST_SECRET = "rate-limit-test-secret-" + ("r" * 64)
PASSWORD = "account password 123"


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _configure_auth(monkeypatch, tmp_path, name="rate-limit.db"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / name}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_SECRET)
    monkeypatch.setenv("AUTH_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("AUTH_ACCESS_TOKEN_MINUTES", "480")
    monkeypatch.setenv("AUTH_JWT_ISSUER", "rate-limit-test")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "rate-limit-web-test")
    monkeypatch.setenv("AUTH_COOKIE_NAME", "salon_access_token")
    monkeypatch.setenv("AUTH_CSRF_COOKIE_NAME", "salon_csrf_token")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTH_COOKIE_SAMESITE", "lax")


def _config(**overrides):
    values = {
        "enabled": True,
        "login_client_limit": 10,
        "login_client_window_seconds": 60,
        "login_client_account_limit": 5,
        "login_client_account_window_seconds": 300,
        "register_client_limit": 10,
        "register_client_window_seconds": 3600,
        "max_buckets": 100,
        "cleanup_interval_seconds": 60,
    }
    values.update(overrides)
    return AuthRateLimitConfig(**values)


def _app(monkeypatch, tmp_path, *, limiter_config=None, clock=None):
    _configure_auth(monkeypatch, tmp_path)
    app = create_app()
    app.state.auth_rate_limiter = AuthRateLimiter(
        limiter_config or _config(),
        clock=clock or FakeClock(),
    )
    return app


def _register(client, email="member@example.com", name="会员", **headers):
    return client.post(
        "/api/auth/register",
        json={"email": email, "display_name": name, "password": PASSWORD},
        headers=headers,
    )


def _login(client, email="member@example.com", password="wrong password 123", **headers):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers=headers,
    )


def test_login_pair_limit_returns_generic_429_before_auth_service(monkeypatch, tmp_path):
    client_host = "198.51.100.10"
    email = "member@example.com"
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    client_key = client_fingerprint(client_host)
    pair_key = login_pair_fingerprint(client_key, account_fingerprint(email))
    app.state.auth_rate_limiter.consume(
        LOGIN_CLIENT_ACCOUNT_SCOPE,
        pair_key,
        1,
        300,
    )

    class ForbiddenAuthService:
        def __init__(self):
            raise AssertionError("blocked login reached AuthService")

    monkeypatch.setattr(auth_api, "AuthService", ForbiddenAuthService)
    with TestClient(app, client=(client_host, 50000)) as client:
        response = _login(client, email=email)

    assert response.status_code == 429
    assert response.json() == {"detail": "请求过于频繁，请稍后再试"}
    assert response.headers["Retry-After"].isdigit()
    assert int(response.headers["Retry-After"]) >= 1


def test_register_limit_returns_429_before_auth_service(monkeypatch, tmp_path):
    client_host = "198.51.100.20"
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(register_client_limit=1),
    )
    app.state.auth_rate_limiter.consume(
        REGISTER_CLIENT_SCOPE,
        client_fingerprint(client_host),
        1,
        3600,
    )

    class ForbiddenAuthService:
        def __init__(self):
            raise AssertionError("blocked registration reached AuthService")

    monkeypatch.setattr(auth_api, "AuthService", ForbiddenAuthService)
    with TestClient(app, client=(client_host, 50000)) as client:
        response = _register(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "请求过于频繁，请稍后再试"}
    assert int(response.headers["Retry-After"]) >= 1


def test_retry_after_uses_strictest_blocked_login_bucket(monkeypatch, tmp_path):
    client_host = "198.51.100.30"
    email = "strict@example.com"
    clock = FakeClock()
    config = _config(
        login_client_limit=1,
        login_client_window_seconds=10,
        login_client_account_limit=1,
        login_client_account_window_seconds=30,
    )
    app = _app(monkeypatch, tmp_path, limiter_config=config, clock=clock)
    client_key = client_fingerprint(client_host)
    pair_key = login_pair_fingerprint(client_key, account_fingerprint(email))
    app.state.auth_rate_limiter.consume(LOGIN_CLIENT_SCOPE, client_key, 1, 10)
    app.state.auth_rate_limiter.consume(LOGIN_CLIENT_ACCOUNT_SCOPE, pair_key, 1, 30)

    with TestClient(app, client=(client_host, 50000)) as client:
        response = _login(client, email=email)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"


def test_failed_login_is_401_success_resets_pair_but_not_client_bucket(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(
            login_client_limit=3,
            login_client_account_limit=2,
        ),
    )
    with TestClient(app, client=("198.51.100.40", 50000)) as client:
        assert _register(client).status_code == 201
        wrong_before = _login(client)
        success = _login(client, password=PASSWORD)
        wrong_after = _login(client)
        client_blocked = _login(client, email="another@example.com")

    assert wrong_before.status_code == 401
    assert wrong_before.json()["detail"] == "邮箱或密码错误"
    assert success.status_code == 200
    assert success.json()["data"]["session_id"]
    assert wrong_after.status_code == 401
    assert client_blocked.status_code == 429


def test_unknown_email_and_wrong_password_keep_same_401_contract(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert _register(client).status_code == 201
        wrong = _login(client)
        unknown = _login(client, email="unknown@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "邮箱或密码错误"}


def test_login_client_limit_applies_across_different_accounts(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_limit=2, login_client_account_limit=10),
    )
    with TestClient(app) as client:
        first = _login(client, email="one@example.com")
        second = _login(client, email="two@example.com")
        third = _login(client, email="three@example.com")

    assert first.status_code == second.status_code == 401
    assert third.status_code == 429


def test_login_pair_limits_are_isolated_by_account(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    with TestClient(app) as client:
        first_a = _login(client, email="a@example.com")
        first_b = _login(client, email="b@example.com")
        second_a = _login(client, email="a@example.com")

    assert first_a.status_code == first_b.status_code == 401
    assert second_a.status_code == 429


def test_login_limits_are_isolated_by_direct_client_address(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    with TestClient(app, client=("192.0.2.10", 50000)) as client_a:
        assert _login(client_a).status_code == 401
        assert _login(client_a).status_code == 429
    with TestClient(app, client=("192.0.2.11", 50000)) as client_b:
        assert _login(client_b).status_code == 401


@pytest.mark.parametrize("header_name", ["X-Forwarded-For", "X-Real-IP"])
def test_spoofed_proxy_headers_cannot_bypass_login_limit(
    monkeypatch,
    tmp_path,
    header_name,
):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    with TestClient(app, client=("192.0.2.20", 50000)) as client:
        first = _login(client, **{header_name: "1.2.3.4"})
        second = _login(client, **{header_name: "5.6.7.8"})

    assert first.status_code == 401
    assert second.status_code == 429


@pytest.mark.parametrize("client_host", ["192.0.2.30", "2001:db8::30"])
def test_login_rate_limit_supports_ipv4_and_ipv6_clients(monkeypatch, tmp_path, client_host):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    with TestClient(app, client=(client_host, 50000)) as client:
        assert _login(client).status_code == 401
        blocked = _login(client)

    assert blocked.status_code == 429


def test_rate_limit_log_contains_no_raw_email_ip_or_fingerprint(
    monkeypatch,
    tmp_path,
    caplog,
):
    client_host = "192.0.2.44"
    email = "private-rate-limit@example.com"
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(login_client_account_limit=1),
    )
    caplog.set_level(logging.WARNING, logger="api.auth")
    with TestClient(app, client=(client_host, 50000)) as client:
        assert _login(client, email=email).status_code == 401
        assert _login(client, email=email).status_code == 429

    text = caplog.text
    assert "operation=login" in text
    assert "status=rate_limited" in text
    assert "scope=login_client_account" in text
    assert email not in text
    assert client_host not in text
    assert account_fingerprint(email) not in text
    assert client_fingerprint(client_host) not in text


def test_registration_success_and_duplicate_both_count_toward_limit(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(register_client_limit=2),
    )
    with TestClient(app) as client:
        success = _register(client)
        duplicate = _register(client, name="重复会员")
        blocked = _register(client, email="other@example.com", name="其他会员")

    assert success.status_code == 201
    assert duplicate.status_code == 409
    assert blocked.status_code == 429


def test_pydantic_rejected_registration_does_not_consume_route_limit(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(register_client_limit=1),
    )
    with TestClient(app) as client:
        invalid = _register(client, email="not-an-email")
        valid = _register(client)
        blocked = _register(client, email="second@example.com")

    assert invalid.status_code == 422
    assert valid.status_code == 201
    assert blocked.status_code == 429


def test_register_limits_are_isolated_and_ignore_forwarded_headers(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(register_client_limit=1),
    )
    with TestClient(app, client=("192.0.2.50", 50000)) as client_a:
        first = _register(client_a, email="a@example.com", name="A")
        spoofed = _register(
            client_a,
            email="b@example.com",
            name="B",
            **{"X-Forwarded-For": "203.0.113.200", "X-Real-IP": "203.0.113.201"},
        )
    with TestClient(app, client=("192.0.2.51", 50000)) as client_b:
        isolated = _register(client_b, email="c@example.com", name="C")

    assert first.status_code == isolated.status_code == 201
    assert spoofed.status_code == 429


def test_missing_request_client_uses_stable_unknown_client_scope():
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": [],
            "client": None,
            "server": ("testserver", 80),
        }
    )

    assert auth_api._client_host(request) is None
    assert client_fingerprint(auth_api._client_host(request)) == client_fingerprint(None)


def test_disabled_rate_limiter_preserves_authentication_behavior(monkeypatch, tmp_path):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(
            enabled=False,
            login_client_limit=1,
            login_client_account_limit=1,
            register_client_limit=1,
        ),
    )
    with TestClient(app) as client:
        registrations = [
            _register(client, email=f"member-{index}@example.com", name=f"会员 {index}")
            for index in range(3)
        ]
        logins = [_login(client, email="missing@example.com") for _ in range(3)]

    assert [response.status_code for response in registrations] == [201, 201, 201]
    assert [response.status_code for response in logins] == [401, 401, 401]


def test_me_and_logout_are_not_affected_by_exhausted_registration_limit(
    monkeypatch,
    tmp_path,
):
    app = _app(
        monkeypatch,
        tmp_path,
        limiter_config=_config(register_client_limit=1),
    )
    with TestClient(app) as client:
        registered = _register(client)
        assert _register(client, email="blocked@example.com").status_code == 429
        me = client.get("/api/auth/me")
        csrf = client.cookies.get("salon_csrf_token")
        logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    assert registered.status_code == 201
    assert me.status_code == 200
    assert logout.status_code == 200


def test_auth_openapi_documents_rate_limit_body_and_retry_after(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    for path in ("/api/auth/login", "/api/auth/register"):
        response = schema["paths"][path]["post"]["responses"]["429"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(
            "/AuthRateLimitResponse"
        )
        assert response["headers"]["Retry-After"]["schema"]["minimum"] == 1


def test_home_auth_form_handles_rate_limits_without_retry_or_secret_storage():
    html = Path("web/templates/index.html").read_text(encoding="utf-8")
    block = html[html.index("authForm.onsubmit"):html.index("logoutBtn.onclick")]

    assert "if (authSubmitting) return" in block
    assert "authSubmitting = true" in block
    assert "authSubmit.disabled = true" in block
    assert "authSubmitting = false" in block
    assert "authSubmit.disabled = false" in block
    assert "response.status === 429" in block
    assert "authRateLimitMessage(response)" in block
    assert "headers.get('Retry-After')" in html
    assert "请求过于频繁，请在 ${retryAfter} 秒后重试" in html
    assert "请求过于频繁，请稍后再试" in html
    assert "setTimeout" not in block
    assert "localStorage" not in block
    assert "console." not in block
    assert "access_token" not in block
    assert 'id="auth-email" name="email" type="email"' in html
    assert 'id="auth-password" name="password" type="password"' in html
    assert 'id="auth-display-name" name="display_name" type="text"' in html
    assert 'id="auth-confirm-password" name="confirm_password" type="password"' in html
