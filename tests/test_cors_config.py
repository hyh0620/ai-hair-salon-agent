from fastapi.middleware.cors import CORSMiddleware


def _cors_middleware(app):
    return [middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware]


def test_cors_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    from app import create_app

    app = create_app()

    assert _cors_middleware(app) == []


def test_cors_uses_explicit_allowlist(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000, https://example.com, ",
    )

    from app import create_app

    app = create_app()
    cors = _cors_middleware(app)

    assert len(cors) == 1
    assert cors[0].kwargs["allow_origins"] == ["http://localhost:3000", "https://example.com"]
    assert cors[0].kwargs["allow_credentials"] is False
