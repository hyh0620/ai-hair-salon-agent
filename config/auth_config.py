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
                480,
                1,
                60 * 24 * 7,
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
            and self.cookie_name != self.csrf_cookie_name
            and self.cookie_samesite in {"lax", "strict", "none"}
            and (self.cookie_samesite != "none" or self.cookie_secure)
        )

    @property
    def max_age_seconds(self) -> int:
        return self.access_token_minutes * 60


def authentication_status() -> str:
    return AuthConfig.from_env().status
