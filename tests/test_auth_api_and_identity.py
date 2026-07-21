import sqlite3
from datetime import timedelta
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

from app import create_app
from config.time_config import time_config
from services.auth_service import AuthService


TEST_SECRET = "test-only-api-secret-" + ("y" * 64)
FUTURE_START = "2035-07-18 14:00"


def _configure_auth(monkeypatch, tmp_path, *, name="auth-api.db", secure=False):
    db_file = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_SECRET)
    monkeypatch.setenv("AUTH_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("AUTH_ACCESS_TOKEN_MINUTES", "480")
    monkeypatch.setenv("AUTH_JWT_ISSUER", "ai-hair-salon-agent-test")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "ai-hair-salon-web-test")
    monkeypatch.setenv("AUTH_COOKIE_NAME", "salon_access_token")
    monkeypatch.setenv("AUTH_CSRF_COOKIE_NAME", "salon_csrf_token")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true" if secure else "false")
    monkeypatch.setenv("AUTH_COOKIE_SAMESITE", "lax")
    return db_file


def _register(client, *, email="UserA@Example.com", name="账户 A", session="guest-session-a"):
    response = client.post(
        "/api/auth/register",
        headers={"X-Chat-Session-ID": session},
        json={
            "email": email,
            "display_name": name,
            "password": "account password 123",
        },
    )
    assert response.status_code == 201, response.text
    return response


def _csrf(client):
    return client.cookies.get("salon_csrf_token")


def _create_payload(**overrides):
    payload = {
        "project": "男士短发",
        "start_time": FUTURE_START,
        "duration": "45分钟",
        "stylist_name": "林浩",
    }
    payload.update(overrides)
    return payload


def test_register_login_me_and_logout_cookie_contract(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client)
        body = registered.json()
        cookies = registered.headers.get_list("set-cookie")

        assert body["data"]["user"]["email"] == "usera@example.com"
        assert body["data"]["user"]["display_name"] == "账户 A"
        assert "password_hash" not in registered.text
        assert body["data"]["token_type"] == "bearer"
        assert body["data"]["access_token"]
        assert body["data"]["session_id"] != "guest-session-a"
        access_cookie = next(item for item in cookies if item.startswith("salon_access_token="))
        csrf_cookie = next(item for item in cookies if item.startswith("salon_csrf_token="))
        assert "HttpOnly" in access_cookie
        assert "SameSite=lax" in access_cookie
        assert "Path=/" in access_cookie
        assert "Secure" not in access_cookie
        assert "HttpOnly" not in csrf_cookie

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["data"]["auth_source"] == "cookie"
        assert me.json()["data"]["user"]["email"] == "usera@example.com"

        wrong = client.post(
            "/api/auth/login",
            json={"email": "usera@example.com", "password": "wrong password 123"},
        )
        malformed_email = client.post(
            "/api/auth/login",
            json={"email": "not-an-email", "password": "wrong password 123"},
        )
        duplicate = client.post(
            "/api/auth/register",
            json={
                "email": "USERA@example.COM",
                "display_name": "重复账户",
                "password": "account password 123",
            },
        )
        short_password = client.post(
            "/api/auth/register",
            json={
                "email": "short@example.com",
                "display_name": "短密码",
                "password": "secret7",
            },
        )
        assert wrong.status_code == malformed_email.status_code == 401
        assert wrong.json()["detail"] == malformed_email.json()["detail"] == "邮箱或密码错误"
        assert duplicate.status_code == 409
        assert short_password.status_code == 422
        assert "secret7" not in short_password.text

        missing_csrf = client.post(
            "/api/auth/logout",
            headers={"X-Chat-Session-ID": body["data"]["session_id"]},
        )
        assert missing_csrf.status_code == 403

        logged_out = client.post(
            "/api/auth/logout",
            headers={
                "X-Chat-Session-ID": body["data"]["session_id"],
                "X-CSRF-Token": _csrf(client),
            },
        )
        assert logged_out.status_code == 200
        assert logged_out.json()["data"]["session_id"] != body["data"]["session_id"]
        assert client.get("/api/auth/me").status_code == 401


def test_secure_cookie_configuration_is_reflected_in_response(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path, secure=True)
    with TestClient(create_app()) as client:
        response = _register(client)

    access_cookie = next(
        item for item in response.headers.get_list("set-cookie")
        if item.startswith("salon_access_token=")
    )
    assert "Secure" in access_cookie
    assert "HttpOnly" in access_cookie


def test_auth_api_is_optional_but_rejects_missing_configuration(monkeypatch, tmp_path):
    db_file = _configure_auth(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_JWT_SECRET", "short")
    with TestClient(create_app()) as client:
        register = client.post(
            "/api/auth/register",
            json={
                "email": "user@example.com",
                "display_name": "用户",
                "password": "account password 123",
            },
        )
        health = client.get("/health")
        guest = client.post(
            "/api/appointment/create",
            json=_create_payload(user_id="guest-still-works"),
        )

    assert register.status_code == 503
    assert health.json()["auth"] == "not_configured"
    assert guest.status_code == 200
    with sqlite3.connect(db_file) as connection:
        owner = connection.execute("SELECT user_id FROM appointments").fetchone()[0]
    assert owner == "guest-still-works"


def test_invalid_expired_and_tampered_tokens_return_401_without_guest_fallback(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client)
        user_id = registered.json()["data"]["user"]["id"]
        valid_token = registered.json()["data"]["access_token"]

        service = AuthService()
        try:
            auth_session_id = jwt.decode(
                valid_token,
                options={"verify_signature": False},
            )["sid"]
            expired_token, _ = service.create_access_token(
                user_id,
                auth_session_id,
                now=time_config.now().astimezone(time_config.BEIJING_TZ) - timedelta(hours=10),
                expires_delta=timedelta(minutes=1),
            )
        finally:
            service.close()

        pieces = valid_token.split(".")
        pieces[2] = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
        tampered_token = ".".join(pieces)
        client.cookies.clear()

        for token in ("not-a-jwt", expired_token, tampered_token):
            response = client.get(
                "/api/appointment",
                params={"user_id": "guest-fallback-must-not-run"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 401
            assert "invalid_token" not in response.text

        client.cookies.set("salon_access_token", "invalid-cookie-token")
        invalid_cookie = client.get(
            "/api/appointment",
            params={"user_id": "guest-fallback-must-not-run"},
        )
        assert invalid_cookie.status_code == 401


def test_bearer_cookie_identity_mismatch_is_rejected(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as account_a, TestClient(app) as account_b:
        _register(account_a, email="a@example.com", name="账户 A")
        registered_b = _register(account_b, email="b@example.com", name="账户 B")
        token_b = registered_b.json()["data"]["access_token"]

        mismatch = account_a.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token_b}"},
        )

    assert mismatch.status_code == 401
    assert "身份不一致" in mismatch.json()["detail"]


def test_cookie_writes_require_csrf_and_authenticated_owner_overrides_client(monkeypatch, tmp_path):
    db_file = _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client)
        account_id = registered.json()["data"]["user"]["id"]
        fake_owner = "account:00000000-0000-4000-8000-000000000001"
        payload = _create_payload(user_id=fake_owner)

        missing_csrf = client.post("/api/appointment/create", json=payload)
        created = client.post(
            "/api/appointment/create",
            json=payload,
            headers={"X-CSRF-Token": _csrf(client)},
        )
        listed = client.get("/api/appointment")

    assert missing_csrf.status_code == 403
    assert created.status_code == 200
    assert created.json()["data"]["user_id"] == f"account:{account_id}"
    assert listed.status_code == 200
    assert listed.json()["data"]["appointments"][0]["owner_id"] == f"account:{account_id}"
    with sqlite3.connect(db_file) as connection:
        owner = connection.execute("SELECT user_id FROM appointments").fetchone()[0]
    assert owner == f"account:{account_id}"


def test_bearer_write_does_not_require_cookie_csrf(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        registered = _register(client)
        token = registered.json()["data"]["access_token"]
        user_id = registered.json()["data"]["user"]["id"]
        client.cookies.clear()

        created = client.post(
            "/api/appointment/create",
            json=_create_payload(),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert created.status_code == 200
    assert created.json()["data"]["user_id"] == f"account:{user_id}"


def test_guest_owner_cannot_enter_account_namespace(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        created = client.post(
            "/api/appointment/create",
            json=_create_payload(
                user_id="account:00000000-0000-4000-8000-000000000001"
            ),
        )
        listed = client.get(
            "/api/appointment",
            params={"user_id": "account:00000000-0000-4000-8000-000000000001"},
        )

    assert created.status_code == listed.status_code == 403


def test_account_ownership_isolated_for_read_cancel_and_update(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as account_a, TestClient(app) as account_b:
        registered_a = _register(account_a, email="a@example.com", name="账户 A")
        _register(account_b, email="b@example.com", name="账户 B")
        created = account_a.post(
            "/api/appointment/create",
            json=_create_payload(),
            headers={"X-CSRF-Token": _csrf(account_a)},
        )
        appointment_id = created.json()["data"]["appointment_id"]
        listed_a = account_a.get("/api/appointment")
        version = listed_a.json()["data"]["appointments"][0]["version"]

        hidden = account_b.get(f"/api/appointment/{appointment_id}")
        cancelled = account_b.post(
            f"/api/appointment/{appointment_id}/cancel",
            json={"expected_version": version},
            headers={"X-CSRF-Token": _csrf(account_b)},
        )
        updated = account_b.patch(
            f"/api/appointment/{appointment_id}",
            json={"expected_version": version, "start_time": "15:00"},
            headers={"X-CSRF-Token": _csrf(account_b)},
        )
        still_visible = account_a.get(f"/api/appointment/{appointment_id}")

    assert registered_a.json()["data"]["user"]["id"]
    assert hidden.status_code == cancelled.status_code == updated.status_code == 404
    assert {hidden.json()["data"]["status"], cancelled.json()["data"]["status"], updated.json()["data"]["status"]} == {"not_found"}
    assert still_visible.status_code == 200
    assert still_visible.json()["data"]["appointment"]["version"] == version


def test_authenticated_update_cancel_and_stale_state_need_no_client_user_id(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    fake_owner = "account:00000000-0000-4000-8000-000000000001"
    with TestClient(create_app()) as client:
        _register(client)
        created = client.post(
            "/api/appointment/create",
            json=_create_payload(),
            headers={"X-CSRF-Token": _csrf(client)},
        )
        appointment_id = created.json()["data"]["appointment_id"]
        version = client.get("/api/appointment").json()["data"]["appointments"][0]["version"]

        update_without_csrf = client.patch(
            f"/api/appointment/{appointment_id}",
            json={"expected_version": version, "start_time": "15:00"},
        )
        updated = client.patch(
            f"/api/appointment/{appointment_id}",
            json={
                "user_id": fake_owner,
                "expected_version": version,
                "start_time": "15:00",
            },
            headers={"X-CSRF-Token": _csrf(client)},
        )
        stale = client.patch(
            f"/api/appointment/{appointment_id}",
            json={"expected_version": version, "start_time": "16:00"},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        cancel_without_csrf = client.post(
            f"/api/appointment/{appointment_id}/cancel",
            json={"expected_version": version + 1},
        )
        cancelled = client.post(
            f"/api/appointment/{appointment_id}/cancel",
            json={"user_id": fake_owner, "expected_version": version + 1},
            headers={"X-CSRF-Token": _csrf(client)},
        )

    assert update_without_csrf.status_code == cancel_without_csrf.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["data"]["appointment"]["version"] == version + 1
    assert updated.json()["data"]["appointment"]["start_time"].endswith("15:00:00")
    assert stale.status_code == 409
    assert stale.json()["data"]["status"] == "stale_state"
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["appointment"]["status"] == "cancelled"
    assert cancelled.json()["data"]["appointment"]["version"] == version + 2


def test_guest_and_account_appointments_are_not_automatically_migrated(monkeypatch, tmp_path):
    db_file = _configure_auth(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app) as guest:
        guest_created = guest.post(
            "/api/appointment/create",
            json=_create_payload(user_id="stable-anonymous-owner"),
        )
    assert guest_created.status_code == 200

    with TestClient(app) as account:
        _register(account, email="member@example.com", name="会员")
        account_list = account.get("/api/appointment")
    assert account_list.status_code == 200
    assert account_list.json()["data"]["appointments"] == []

    with sqlite3.connect(db_file) as connection:
        owners = [row[0] for row in connection.execute("SELECT user_id FROM appointments")]
    assert owners == ["stable-anonymous-owner"]


def test_account_chat_uses_trusted_owner_and_cookie_csrf(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    captured = []

    async def fake_stream(
        message,
        *,
        session_id=None,
        owner_id=None,
        owner_authenticated=False,
        route=None,
    ):
        captured.append((message, session_id, owner_id, owner_authenticated, route))
        yield "[REPLY]测试完成"

    monkeypatch.setattr("web.routes.ProcessUserInput_stream", fake_stream)
    with TestClient(create_app()) as client:
        registered = _register(client)
        account_id = registered.json()["data"]["user"]["id"]
        csrf = _csrf(client)
        for message in ("预约明天下午两点", "查看我的预约", "修改预约", "取消预约"):
            missing_csrf = client.post(
                "/chat/stream",
                json={
                    "message": message,
                    "session_id": "account-chat-session",
                    "owner_id": "account:00000000-0000-4000-8000-000000000001",
                    "route": "appointment",
                },
            )
            assert missing_csrf.status_code == 403
            response = client.post(
                "/chat/stream",
                json={
                    "message": message,
                    "session_id": "account-chat-session",
                    "owner_id": "account:00000000-0000-4000-8000-000000000001",
                    "route": "appointment",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 200

        reset_missing = client.post(
            "/api/chat/reset",
            json={"session_id": "account-chat-session"},
        )
        reset = client.post(
            "/api/chat/reset",
            json={"session_id": "account-chat-session"},
            headers={"X-CSRF-Token": csrf},
        )

    assert reset_missing.status_code == 403
    assert reset.status_code == 200
    assert reset.json()["session_id"] != "account-chat-session"
    assert len(captured) == 4
    assert {item[2] for item in captured} == {f"account:{account_id}"}
    assert all(item[3] is True for item in captured)


def test_account_ui_health_status_and_openapi_security(monkeypatch, tmp_path):
    _configure_auth(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        home = client.get("/")
        health = client.get("/health")
        status_page = client.get("/status")
        schema = client.get("/openapi.json").json()

    assert home.status_code == 200
    assert "当前为游客模式" in home.text
    assert 'id="login-btn"' in home.text
    assert 'id="register-btn"' in home.text
    assert "现有游客预约不会自动转移" in home.text
    assert "localStorage.setItem('access_token'" not in home.text
    assert 'localStorage.setItem("access_token"' not in home.text
    assert "sessionStorage.setItem" not in home.text
    assert health.json()["auth"] == "configured"
    assert "账户认证" in status_page.text
    assert "configured" in status_page.text
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/api/auth/register" in schema["paths"]
    assert "/api/auth/login" in schema["paths"]
    assert "/api/auth/logout" in schema["paths"]
    assert "/api/auth/me" in schema["paths"]


def test_home_script_preserves_anonymous_owner_across_identity_switches():
    html = Path("web/templates/index.html").read_text(encoding="utf-8")
    register_login_block = html[html.index("authForm.onsubmit"):html.index("logoutBtn.onclick")]
    logout_block = html[html.index("logoutBtn.onclick"):html.index("// 添加回车键")]

    assert "salon_anonymous_owner_id" in html
    assert "setSessionId(payload.data.session_id)" in register_login_block
    assert "setSessionId(payload.data.session_id)" in logout_block
    assert "removeItem(ownerStorageKey)" not in html
    assert "setItem(ownerStorageKey" not in register_login_block
    assert "setItem(ownerStorageKey" not in logout_block
    assert "access_token" not in html
