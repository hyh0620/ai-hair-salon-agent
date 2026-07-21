"""Bounded environment configuration for authentication rate limiting."""

from __future__ import annotations

from dataclasses import dataclass
import os

from config.external_calls import load_runtime_dotenv


load_runtime_dotenv()

MIN_LIMIT = 1
MAX_LIMIT = 10_000
MIN_WINDOW_SECONDS = 1
MAX_WINDOW_SECONDS = 60 * 60 * 24 * 30
MIN_BUCKETS = 1
MAX_BUCKETS = 100_000
MIN_CLEANUP_INTERVAL_SECONDS = 1
MAX_CLEANUP_INTERVAL_SECONDS = 60 * 60 * 24


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class AuthRateLimitConfig:
    enabled: bool = True
    login_client_limit: int = 10
    login_client_window_seconds: int = 60
    login_client_account_limit: int = 5
    login_client_account_window_seconds: int = 300
    register_client_limit: int = 3
    register_client_window_seconds: int = 3600
    refresh_client_limit: int = 30
    refresh_client_window_seconds: int = 60
    max_buckets: int = 10_000
    cleanup_interval_seconds: int = 60

    def __post_init__(self) -> None:
        _validate_range("login_client_limit", self.login_client_limit, MIN_LIMIT, MAX_LIMIT)
        _validate_range(
            "login_client_window_seconds",
            self.login_client_window_seconds,
            MIN_WINDOW_SECONDS,
            MAX_WINDOW_SECONDS,
        )
        _validate_range(
            "login_client_account_limit",
            self.login_client_account_limit,
            MIN_LIMIT,
            MAX_LIMIT,
        )
        _validate_range(
            "login_client_account_window_seconds",
            self.login_client_account_window_seconds,
            MIN_WINDOW_SECONDS,
            MAX_WINDOW_SECONDS,
        )
        _validate_range("register_client_limit", self.register_client_limit, MIN_LIMIT, MAX_LIMIT)
        _validate_range(
            "register_client_window_seconds",
            self.register_client_window_seconds,
            MIN_WINDOW_SECONDS,
            MAX_WINDOW_SECONDS,
        )
        _validate_range(
            "refresh_client_limit",
            self.refresh_client_limit,
            MIN_LIMIT,
            MAX_LIMIT,
        )
        _validate_range(
            "refresh_client_window_seconds",
            self.refresh_client_window_seconds,
            MIN_WINDOW_SECONDS,
            MAX_WINDOW_SECONDS,
        )
        _validate_range("max_buckets", self.max_buckets, MIN_BUCKETS, MAX_BUCKETS)
        _validate_range(
            "cleanup_interval_seconds",
            self.cleanup_interval_seconds,
            MIN_CLEANUP_INTERVAL_SECONDS,
            MAX_CLEANUP_INTERVAL_SECONDS,
        )

    @classmethod
    def from_env(cls) -> "AuthRateLimitConfig":
        return cls(
            enabled=_env_bool("AUTH_RATE_LIMIT_ENABLED", True),
            login_client_limit=_bounded_env_int(
                "AUTH_LOGIN_CLIENT_LIMIT", 10, MIN_LIMIT, MAX_LIMIT
            ),
            login_client_window_seconds=_bounded_env_int(
                "AUTH_LOGIN_CLIENT_WINDOW_SECONDS",
                60,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            login_client_account_limit=_bounded_env_int(
                "AUTH_LOGIN_CLIENT_ACCOUNT_LIMIT", 5, MIN_LIMIT, MAX_LIMIT
            ),
            login_client_account_window_seconds=_bounded_env_int(
                "AUTH_LOGIN_CLIENT_ACCOUNT_WINDOW_SECONDS",
                300,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            register_client_limit=_bounded_env_int(
                "AUTH_REGISTER_CLIENT_LIMIT", 3, MIN_LIMIT, MAX_LIMIT
            ),
            register_client_window_seconds=_bounded_env_int(
                "AUTH_REGISTER_CLIENT_WINDOW_SECONDS",
                3600,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            refresh_client_limit=_bounded_env_int(
                "AUTH_REFRESH_CLIENT_LIMIT", 30, MIN_LIMIT, MAX_LIMIT
            ),
            refresh_client_window_seconds=_bounded_env_int(
                "AUTH_REFRESH_CLIENT_WINDOW_SECONDS",
                60,
                MIN_WINDOW_SECONDS,
                MAX_WINDOW_SECONDS,
            ),
            max_buckets=_bounded_env_int(
                "AUTH_RATE_LIMIT_MAX_BUCKETS",
                10_000,
                MIN_BUCKETS,
                MAX_BUCKETS,
            ),
            cleanup_interval_seconds=_bounded_env_int(
                "AUTH_RATE_LIMIT_CLEANUP_INTERVAL_SECONDS",
                60,
                MIN_CLEANUP_INTERVAL_SECONDS,
                MAX_CLEANUP_INTERVAL_SECONDS,
            ),
        )


def _validate_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
