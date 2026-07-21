import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from config.time_config import time_config
from services.auth_service import AuthService


TEST_SECRET = "test-only-user-analysis-secret-" + ("z" * 64)


def _configure(monkeypatch, tmp_path, name="user-analysis.db"):
    db_file = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_SECRET)
    monkeypatch.setenv("AUTH_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("AUTH_ACCESS_TOKEN_MINUTES", "480")
    monkeypatch.setenv("AUTH_JWT_ISSUER", "user-analysis-test")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "user-analysis-web-test")
    monkeypatch.setenv("AUTH_COOKIE_NAME", "salon_access_token")
    monkeypatch.setenv("AUTH_CSRF_COOKIE_NAME", "salon_csrf_token")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTH_COOKIE_SAMESITE", "lax")
    return db_file


def _register(client, email, display_name):
    response = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "display_name": display_name,
            "password": "account password 123",
        },
    )
    assert response.status_code == 201, response.text
    return response


def _csrf(client):
    return client.cookies.get("salon_csrf_token")


def _booking(start_time, *, user_id=None):
    payload = {
        "project": "男士短发",
        "start_time": start_time,
        "duration": "45分钟",
        "stylist_name": "林浩",
    }
    if user_id is not None:
        payload["user_id"] = user_id
    return payload


def test_account_guest_analysis_and_behavior_writes_are_identity_scoped(
    monkeypatch,
    tmp_path,
):
    db_file = _configure(monkeypatch, tmp_path)
    app = create_app()

    with TestClient(app) as account_a, TestClient(app) as account_b:
        registered_a = _register(account_a, "a@example.com", "账户 A")
        account_a_id = registered_a.json()["data"]["user"]["id"]
        _register(account_b, "b@example.com", "账户 B")

        created_a = account_a.post(
            "/api/appointment/create",
            json=_booking("2035-07-18 14:00", user_id="forged-owner"),
            headers={"X-CSRF-Token": _csrf(account_a)},
        )
        duplicate_a = account_a.post(
            "/api/appointment/create",
            json=_booking("2035-07-18 14:00"),
            headers={"X-CSRF-Token": _csrf(account_a)},
        )
        analysis_a = account_a.get("/api/user-behavior/analysis")
        analysis_b = account_b.get(
            "/api/user-behavior/analysis",
            params={"user_id": f"account:{account_a_id}"},
            headers={"X-Anonymous-Owner-ID": "forged-guest-owner"},
        )

    with TestClient(app) as guest:
        created_guest = guest.post(
            "/api/appointment/create",
            json=_booking("2035-07-19 14:00", user_id="browser-guest-owner"),
        )
        analysis_guest = guest.get(
            "/api/user-behavior/analysis",
            headers={"X-Anonymous-Owner-ID": "browser-guest-owner"},
        )

    assert created_a.status_code == created_guest.status_code == 200
    assert duplicate_a.status_code == 409
    assert analysis_a.status_code == analysis_b.status_code == analysis_guest.status_code == 200
    assert analysis_a.json()["total_appointments"] == 1
    assert analysis_a.json()["viewer"] == {
        "mode": "account",
        "display_name": "账户 A",
    }
    assert analysis_b.json()["total_appointments"] == 0
    assert analysis_b.json()["viewer"]["display_name"] == "账户 B"
    assert analysis_guest.json()["total_appointments"] == 1
    assert analysis_guest.json()["viewer"] == {
        "mode": "anonymous",
        "display_name": "游客",
    }

    public_payloads = json.dumps(
        [analysis_a.json(), analysis_b.json(), analysis_guest.json()],
        ensure_ascii=False,
    )
    assert account_a_id not in public_payloads
    assert "account:" not in public_payloads
    assert "browser-guest-owner" not in public_payloads
    assert "password_hash" not in public_payloads

    with sqlite3.connect(db_file) as connection:
        behavior_rows = connection.execute(
            "SELECT user_id, session_id FROM user_behaviors "
            "WHERE action_type='appointment' ORDER BY id"
        ).fetchall()
    assert [row[0] for row in behavior_rows] == [
        f"account:{account_a_id}",
        "browser-guest-owner",
    ]
    assert behavior_rows[0][1].startswith("api-")
    assert behavior_rows[0][1] != behavior_rows[0][0]
    assert all(row[0] != "default_user" for row in behavior_rows)


def test_guest_history_is_not_claimed_and_returns_after_logout(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "guest-return.db")
    with TestClient(create_app()) as client:
        guest_created = client.post(
            "/api/appointment/create",
            json=_booking("2035-08-01 14:00", user_id="persistent-browser-guest"),
        )
        registered = _register(client, "member@example.com", "会员账户")
        account_analysis = client.get(
            "/api/user-behavior/analysis",
            headers={"X-Anonymous-Owner-ID": "persistent-browser-guest"},
        )
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": _csrf(client)},
        )
        guest_analysis = client.get(
            "/api/user-behavior/analysis",
            headers={"X-Anonymous-Owner-ID": "persistent-browser-guest"},
        )

    assert guest_created.status_code == 200
    assert registered.status_code == 201
    assert account_analysis.json()["total_appointments"] == 0
    assert account_analysis.json()["viewer"]["mode"] == "account"
    assert logout.status_code == 200
    assert guest_analysis.json()["total_appointments"] == 1
    assert guest_analysis.json()["viewer"]["mode"] == "anonymous"


def test_analysis_compatibility_routes_share_validated_guest_identity(
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path, "compatibility.db")
    headers = {"X-Anonymous-Owner-ID": "compatibility-guest"}
    with TestClient(create_app()) as client:
        responses = [
            client.get("/api/user-behavior/analysis", headers=headers),
            client.get("/api/user-behavior/dashboard_data", headers=headers),
            client.get("/api/user_behavior/dashboard_data", headers=headers),
        ]
        missing = client.get("/api/user-behavior/analysis")
        reserved = client.get(
            "/api/user-behavior/analysis",
            headers={
                "X-Anonymous-Owner-ID": (
                    "account:00000000-0000-4000-8000-000000000001"
                )
            },
        )

    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert responses[0].json()["viewer"]["display_name"] == "游客"
    assert missing.status_code == 422
    assert reserved.status_code == 403


def test_invalid_and_expired_jwt_never_fall_back_to_guest(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "invalid-token.db")
    with TestClient(create_app()) as client:
        registered = _register(client, "token@example.com", "Token 用户")
        user_id = registered.json()["data"]["user"]["id"]
        service = AuthService()
        try:
            expired, _ = service.create_access_token(
                user_id,
                now=time_config.now() - timedelta(hours=10),
                expires_delta=timedelta(minutes=1),
            )
        finally:
            service.close()
        client.cookies.clear()

        responses = [
            client.get(
                "/api/user-behavior/analysis",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Anonymous-Owner-ID": "must-not-fallback",
                },
            )
            for token in ("not-a-jwt", expired)
        ]

    assert all(response.status_code == 401 for response in responses)


def test_reminder_uses_trusted_identity_csrf_and_bearer(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "reminder.db")
    captured = []

    async def fake_reminder(self, owner_id, display_name=None):
        captured.append((owner_id, display_name))
        return {"message": "回访建议", "stylist_available_times": []}

    monkeypatch.setattr(
        "agents.user_behavior_agent.UserBehaviorAgent.get_reminder_with_schedule",
        fake_reminder,
    )
    app = create_app()
    with TestClient(app) as account:
        registered = _register(account, "reminder@example.com", "提醒用户")
        account_id = registered.json()["data"]["user"]["id"]
        access_token = registered.json()["data"]["access_token"]
        missing_csrf = account.post(
            "/api/user-behavior/send-reminder",
            json={"user_id": "forged-owner"},
        )
        cookie_response = account.post(
            "/api/user-behavior/send-reminder",
            headers={
                "X-CSRF-Token": _csrf(account),
                "X-Anonymous-Owner-ID": "forged-guest-owner",
            },
            json={"user_id": "forged-owner"},
        )
        account.cookies.clear()
        bearer_response = account.post(
            "/api/user-behavior/send-reminder",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    with TestClient(app) as guest:
        guest_response = guest.post(
            "/api/user-behavior/send-reminder",
            headers={"X-Anonymous-Owner-ID": "reminder-guest"},
            json={"user_id": f"account:{account_id}"},
        )

    assert missing_csrf.status_code == 403
    assert cookie_response.status_code == bearer_response.status_code == 200
    assert guest_response.status_code == 200
    assert captured == [
        (f"account:{account_id}", "提醒用户"),
        (f"account:{account_id}", "提醒用户"),
        ("reminder-guest", None),
    ]
    assert all("owner" not in response.text for response in (cookie_response, guest_response))


def test_reminder_failure_is_neutral_and_does_not_leak_exception(
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path, "reminder-failure.db")

    async def failing_reminder(self, owner_id, display_name=None):
        raise RuntimeError("internal reminder detail")

    monkeypatch.setattr(
        "agents.user_behavior_agent.UserBehaviorAgent.get_reminder_with_schedule",
        failing_reminder,
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/user-behavior/send-reminder",
            headers={"X-Anonymous-Owner-ID": "failure-guest"},
        )

    assert response.status_code == 200
    assert "Tom" not in response.text
    assert "internal reminder detail" not in response.text
    assert "系统暂时无法生成回访建议" in response.json()["message"]


def test_account_form_and_user_analysis_page_contract(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "page-contract.db")
    with TestClient(create_app()) as client:
        home = client.get("/")
        analysis_page = client.get("/user_behavior")
        alias_page = client.get("/user_behavior_analysis")

    assert home.status_code == analysis_page.status_code == alias_page.status_code == 200
    home_html = home.text
    page_html = analysis_page.text

    assert ".auth-field[hidden]" in home_html
    assert "display: none !important" in home_html
    assert 'id="auth-email" name="email" type="email"' in home_html
    assert 'id="auth-email"' in home_html and "required" in home_html
    assert 'id="auth-password"' in home_html and 'autocomplete="current-password"' in home_html
    assert 'id="auth-display-name"' in home_html and "disabled" in home_html
    assert 'id="auth-confirm-password"' in home_html and "disabled" in home_html
    assert "authDisplayName.disabled = !registering" in home_html
    assert "authConfirmPassword.disabled = !registering" in home_html
    assert "authDisplayName.required = registering" in home_html
    assert "authConfirmPassword.required = registering" in home_html
    assert "authPassword.autocomplete = registering ? 'new-password' : 'current-password'" in home_html
    assert "authForm.reset()" in home_html
    assert "authPassword.value !== authConfirmPassword.value" in home_html
    assert "const body = {email: authEmail.value.trim(), password: authPassword.value}" in home_html
    assert "if (authMode === 'register') body.display_name" in home_html
    assert "localStorage.setItem('access_token'" not in home_html
    assert "localStorage.setItem('password'" not in home_html

    assert page_html == alias_page.text
    assert "default_user" not in page_html
    assert "account:<" not in page_html
    assert "http://127.0.0.1:8000" not in page_html
    assert "调试：" not in page_html
    assert "console.log" not in page_html
    assert "fetch('/api/user-behavior/analysis'" in page_html
    assert "fetch('/api/user-behavior/send-reminder'" in page_html
    assert "credentials: 'same-origin'" in page_html
    assert "X-Anonymous-Owner-ID" in page_html
    assert "X-CSRF-Token" in page_html
    assert "user_id:" not in page_html
    assert "个人偏好概览" in page_html
    assert page_html.count("基于历史预约数据的智能分析") == 1
    assert "暂无数据" in page_html
    viewer_label_position = page_html.index(
        'class="viewer-label">当前分析用户'
    )
    viewer_name_position = page_html.index('id="currentViewerName"')
    viewer_mode_position = page_html.index('id="currentViewerMode"')
    assert viewer_label_position < viewer_name_position < viewer_mode_position
    assert 'id="currentViewerName" class="viewer-name">游客' in page_html
    assert "当前浏览器访客" in page_html
    assert "? '账户用户'" in page_html
    assert 'href="/" class="nav-btn"' in page_html
    assert "返回主界面" in page_html
    assert "@media (max-width: 420px)" in page_html


def test_user_analysis_openapi_has_viewer_and_no_client_identity_input(
    monkeypatch,
    tmp_path,
):
    _configure(monkeypatch, tmp_path, "openapi.db")
    with TestClient(create_app()) as client:
        schema = client.get("/openapi.json").json()

    analysis = schema["paths"]["/api/user-behavior/analysis"]["get"]
    reminder = schema["paths"]["/api/user-behavior/send-reminder"]["post"]
    assert "UserAnalysisViewer" in schema["components"]["schemas"]
    assert all(parameter["name"] != "user_id" for parameter in analysis.get("parameters", []))
    assert "requestBody" not in reminder
