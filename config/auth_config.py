"""Environment-backed configuration for optional account authentication."""

from __future__ import annotations

from dataclasses import dataclass, field
import os

from config.external_calls import load_runtime_dotenv


load_runtime_dotenv()

MIN_JWT_SECRET_BYTES = 32
SUPPORTED_JWT_ALGORITHM = "HS256"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    jwt_secret: str = field(repr=False)
    jwt_algorithm: str
    access_token_minutes: int
    issuer: str
    audience: str
    cookie_name: str
    csrf_cookie_name: str
    cookie_secure: bool
    cookie_samesite: str
    refresh_token_days: int = 30
    refresh_cookie_name: str = "salon_refresh_token"
    refresh_cookie_path: str = "/api/auth"
    refresh_reuse_grace_seconds: int = 3
    auth_session_retention_days: int = 30

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            enabled=_env_bool("AUTH_ENABLED", False),
            jwt_secret=os.getenv("AUTH_JWT_SECRET", ""),
            jwt_algorithm=os.getenv(
                "AUTH_JWT_ALGORITHM",
                SUPPORTED_JWT_ALGORITHM,
            ).strip(),
            access_token_minutes=_env_int(
                "AUTH_ACCESS_TOKEN_MINUTES",
                15,
                1,
                60 * 24,
            ),
            issuer=os.getenv(
                "AUTH_JWT_ISSUER",
                "ai-hair-salon-agent",
            ).strip(),
            audience=os.getenv(
                "AUTH_JWT_AUDIENCE",
                "ai-hair-salon-web",
            ).strip(),
            cookie_name=os.getenv(
                "AUTH_COOKIE_NAME",
                "salon_access_token",
            ).strip(),
            csrf_cookie_name=os.getenv(
                "AUTH_CSRF_COOKIE_NAME",
                "salon_csrf_token",
            ).strip(),
            cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
            cookie_samesite=os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower(),
            refresh_token_days=_env_int(
                "AUTH_REFRESH_TOKEN_DAYS",
                30,
                1,
                90,
            ),
            refresh_cookie_name=os.getenv(
                "AUTH_REFRESH_COOKIE_NAME",
                "salon_refresh_token",
            ).strip(),
            refresh_cookie_path=os.getenv(
                "AUTH_REFRESH_COOKIE_PATH",
                "/api/auth",
            ).strip(),
            refresh_reuse_grace_seconds=_env_int(
                "AUTH_REFRESH_REUSE_GRACE_SECONDS",
                3,
                1,
                60,
            ),
            auth_session_retention_days=_env_int(
                "AUTH_AUTH_SESSION_RETENTION_DAYS",
                30,
                1,
                365,
            ),
        )

    @property
    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.is_configured:
            return "not_configured"
        return "configured"

    @property
    def is_configured(self) -> bool:
        secret_bytes = self.jwt_secret.strip().encode("utf-8")
        return bool(
            self.enabled
            and len(secret_bytes) >= MIN_JWT_SECRET_BYTES
            and self.jwt_algorithm == SUPPORTED_JWT_ALGORITHM
            and self.issuer
            and self.audience
            and self.cookie_name
            and self.csrf_cookie_name
            and self.refresh_cookie_name
            and len(
                {
                    self.cookie_name,
                    self.csrf_cookie_name,
                    self.refresh_cookie_name,
                }
            )
            == 3
            and self.refresh_cookie_path.startswith("/")
            and ";" not in self.refresh_cookie_path
            and 1 <= self.access_token_minutes <= 60 * 24
            and 1 <= self.refresh_token_days <= 90
            and 1 <= self.refresh_reuse_grace_seconds <= 60
            and 1 <= self.auth_session_retention_days <= 365
            and self.cookie_samesite in {"lax", "strict", "none"}
            and (self.cookie_samesite != "none" or self.cookie_secure)
        )

    @property
    def max_age_seconds(self) -> int:
        return self.access_token_minutes * 60

    @property
    def refresh_max_age_seconds(self) -> int:
        return self.refresh_token_days * 24 * 60 * 60


def authentication_status() -> str:
    return AuthConfig.from_env().status
