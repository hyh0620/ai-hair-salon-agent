from pathlib import Path


def test_shared_auth_fetch_uses_single_flight_and_one_business_retry():
    source = Path("web/static/auth_refresh.js").read_text(encoding="utf-8")

    assert "let refreshPromise = null" in source
    assert "refreshSingleFlight" in source
    assert "if (!refreshPromise)" in source
    assert "refreshPromise = performRefresh().finally" in source
    assert "window.fetch('/api/auth/refresh'" in source
    assert "return window.fetch(withCurrentCsrf(retryRequest))" in source
    assert source.count("window.fetch(withCurrentCsrf(retryRequest))") == 1
    assert "headers.set('X-CSRF-Token', token)" in source
    assert "response.status !== 401" in source
    assert "response.status !== 409 || attempt === 1" in source
    assert "attempt < 2" in source


def test_auth_endpoints_are_excluded_from_recursive_refresh():
    source = Path("web/static/auth_refresh.js").read_text(encoding="utf-8")

    for path in (
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/refresh",
        "/api/auth/logout",
    ):
        assert f"'{path}'" in source
    assert "excludedAuthPaths.has(url.pathname)" in source


def test_frontend_never_reads_or_persists_refresh_credentials():
    source = Path("web/static/auth_refresh.js").read_text(encoding="utf-8")
    templates = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "web/templates/index.html",
            "web/templates/user_behavior_analysis.html",
            "web/templates/knowledge_management.html",
        )
    )

    assert "document.cookie" in source
    assert "csrfCookieName" in source
    assert "salon_refresh_token" not in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "access_token" not in source
    assert "refresh_token" not in source
    assert "SalonAuthRefresh.fetch" in templates
    assert "localStorage.setItem('access" not in templates
    assert "localStorage.setItem('refresh" not in templates


def test_all_existing_page_business_fetches_use_shared_refresh_wrapper():
    index = Path("web/templates/index.html").read_text(encoding="utf-8")
    behavior = Path("web/templates/user_behavior_analysis.html").read_text(encoding="utf-8")
    knowledge = Path("web/templates/knowledge_management.html").read_text(encoding="utf-8")

    for path in (
        "/api/auth/me",
        "/api/chat/route",
        "/api/consultation/query",
        "/chat/stream",
        "/api/chat/reset",
    ):
        position = index.index(path)
        assert "SalonAuthRefresh.fetch" in index[max(0, position - 80):position]
    assert "SalonAuthRefresh.fetch('/api/user-behavior/analysis'" in behavior
    assert "SalonAuthRefresh.fetch('/api/user-behavior/send-reminder'" in behavior
    assert "SalonAuthRefresh.fetch('/api/knowledge/'" in knowledge
    assert "SalonAuthRefresh.fetch('/api/knowledge/reconnect'" in knowledge
    assert "fetch(endpoint" in index
    assert "fetch('/api/auth/logout'" in index


def test_refresh_failure_notifies_login_expiry_without_touching_guest_owner():
    source = Path("web/static/auth_refresh.js").read_text(encoding="utf-8")
    index = Path("web/templates/index.html").read_text(encoding="utf-8")

    assert "salon-auth-expired" in source
    assert "登录状态已失效，请重新登录" in source
    assert "salon-auth-expired" in index
    listener = index[index.index("window.addEventListener('salon-auth-expired'"):]
    listener = listener[:listener.index("// 添加回车键发送功能")]
    assert "setAccountState(null)" in listener
    assert "removeItem" not in listener
    assert "ownerStorageKey" not in listener
