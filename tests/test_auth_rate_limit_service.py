from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time

import pytest

from config.auth_rate_limit_config import (
    MAX_BUCKETS,
    MAX_LIMIT,
    MAX_WINDOW_SECONDS,
    AuthRateLimitConfig,
)
from services.auth_rate_limit_service import (
    LOGIN_CLIENT_ACCOUNT_SCOPE,
    LOGIN_CLIENT_SCOPE,
    REGISTER_CLIENT_SCOPE,
    AuthRateLimiter,
    account_fingerprint,
    client_fingerprint,
    login_pair_fingerprint,
    normalize_client_address,
)


RATE_LIMIT_ENV_NAMES = (
    "AUTH_RATE_LIMIT_ENABLED",
    "AUTH_LOGIN_CLIENT_LIMIT",
    "AUTH_LOGIN_CLIENT_WINDOW_SECONDS",
    "AUTH_LOGIN_CLIENT_ACCOUNT_LIMIT",
    "AUTH_LOGIN_CLIENT_ACCOUNT_WINDOW_SECONDS",
    "AUTH_REGISTER_CLIENT_LIMIT",
    "AUTH_REGISTER_CLIENT_WINDOW_SECONDS",
    "AUTH_RATE_LIMIT_MAX_BUCKETS",
    "AUTH_RATE_LIMIT_CLEANUP_INTERVAL_SECONDS",
)


@dataclass
class FakeClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _clear_rate_limit_env(monkeypatch) -> None:
    for name in RATE_LIMIT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_rate_limit_config_defaults_to_enabled_and_expected_limits(monkeypatch):
    _clear_rate_limit_env(monkeypatch)

    config = AuthRateLimitConfig.from_env()

    assert config.enabled is True
    assert (config.login_client_limit, config.login_client_window_seconds) == (10, 60)
    assert (
        config.login_client_account_limit,
        config.login_client_account_window_seconds,
    ) == (5, 300)
    assert (config.register_client_limit, config.register_client_window_seconds) == (
        3,
        3600,
    )
    assert config.max_buckets == 10_000
    assert config.cleanup_interval_seconds == 60


def test_rate_limit_config_reads_explicit_values_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_LIMIT", "8")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_WINDOW_SECONDS", "45")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_ACCOUNT_LIMIT", "4")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_ACCOUNT_WINDOW_SECONDS", "240")
    monkeypatch.setenv("AUTH_REGISTER_CLIENT_LIMIT", "2")
    monkeypatch.setenv("AUTH_REGISTER_CLIENT_WINDOW_SECONDS", "1800")
    monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_BUCKETS", "500")
    monkeypatch.setenv("AUTH_RATE_LIMIT_CLEANUP_INTERVAL_SECONDS", "30")

    config = AuthRateLimitConfig.from_env()

    assert config == AuthRateLimitConfig(
        enabled=False,
        login_client_limit=8,
        login_client_window_seconds=45,
        login_client_account_limit=4,
        login_client_account_window_seconds=240,
        register_client_limit=2,
        register_client_window_seconds=1800,
        max_buckets=500,
        cleanup_interval_seconds=30,
    )


def test_rate_limit_config_clamps_unsafe_values_and_uses_safe_defaults(monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "invalid")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_LIMIT", "-20")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_WINDOW_SECONDS", "0")
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_ACCOUNT_LIMIT", str(MAX_LIMIT + 1))
    monkeypatch.setenv("AUTH_LOGIN_CLIENT_ACCOUNT_WINDOW_SECONDS", "not-an-int")
    monkeypatch.setenv("AUTH_REGISTER_CLIENT_LIMIT", str(MAX_LIMIT * 10))
    monkeypatch.setenv("AUTH_REGISTER_CLIENT_WINDOW_SECONDS", str(MAX_WINDOW_SECONDS + 1))
    monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_BUCKETS", str(MAX_BUCKETS + 1))
    monkeypatch.setenv("AUTH_RATE_LIMIT_CLEANUP_INTERVAL_SECONDS", "-1")

    config = AuthRateLimitConfig.from_env()

    assert config.enabled is True
    assert config.login_client_limit == 1
    assert config.login_client_window_seconds == 1
    assert config.login_client_account_limit == MAX_LIMIT
    assert config.login_client_account_window_seconds == 300
    assert config.register_client_limit == MAX_LIMIT
    assert config.register_client_window_seconds == MAX_WINDOW_SECONDS
    assert config.max_buckets == MAX_BUCKETS
    assert config.cleanup_interval_seconds == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"login_client_limit": 0},
        {"login_client_window_seconds": 0},
        {"login_client_account_limit": -1},
        {"register_client_window_seconds": MAX_WINDOW_SECONDS + 1},
        {"max_buckets": 0},
        {"cleanup_interval_seconds": 0},
    ],
)
def test_direct_rate_limit_config_rejects_out_of_range_values(overrides):
    with pytest.raises(ValueError):
        AuthRateLimitConfig(**overrides)


def test_limiter_uses_monotonic_clock_by_default():
    limiter = AuthRateLimiter()
    assert limiter._clock is time.monotonic


def test_sliding_window_allows_until_limit_then_recovers_without_sleep():
    clock = FakeClock()
    limiter = AuthRateLimiter(AuthRateLimitConfig(), clock=clock)

    assert limiter.consume("scope", "key", 2, 10).allowed is True
    assert limiter.consume("scope", "key", 2, 10).allowed is True
    blocked = limiter.consume("scope", "key", 2, 10)
    assert blocked.allowed is False
    assert blocked.retry_after_seconds == 10

    clock.advance(9.2)
    almost_ready = limiter.consume("scope", "key", 2, 10)
    assert almost_ready.allowed is False
    assert almost_ready.retry_after_seconds == 1

    clock.advance(0.8)
    assert limiter.consume("scope", "key", 2, 10).allowed is True


def test_scope_and_key_isolation_reset_and_clear():
    limiter = AuthRateLimiter(AuthRateLimitConfig())

    assert limiter.consume(LOGIN_CLIENT_SCOPE, "client-a", 1, 60).allowed
    assert not limiter.consume(LOGIN_CLIENT_SCOPE, "client-a", 1, 60).allowed
    assert limiter.consume(LOGIN_CLIENT_SCOPE, "client-b", 1, 60).allowed
    assert limiter.consume(REGISTER_CLIENT_SCOPE, "client-a", 1, 60).allowed
    assert limiter.bucket_count == 3

    assert limiter.reset(LOGIN_CLIENT_SCOPE, "client-a") is True
    assert limiter.reset(LOGIN_CLIENT_SCOPE, "client-a") is False
    assert limiter.consume(LOGIN_CLIENT_SCOPE, "client-a", 1, 60).allowed

    limiter.clear()
    assert limiter.bucket_count == 0


def test_expired_buckets_are_pruned_and_empty_buckets_removed():
    clock = FakeClock()
    config = AuthRateLimitConfig(cleanup_interval_seconds=2)
    limiter = AuthRateLimiter(config, clock=clock)
    limiter.consume("scope", "expired", 1, 5)
    limiter.consume("scope", "active", 1, 20)

    clock.advance(6)
    assert limiter.prune() == 1
    assert limiter.bucket_count == 1
    assert limiter.reset("scope", "expired") is False
    assert limiter.reset("scope", "active") is True


def test_capacity_is_bounded_and_evicts_least_recently_used_bucket():
    clock = FakeClock()
    config = AuthRateLimitConfig(max_buckets=2)
    limiter = AuthRateLimiter(config, clock=clock)

    limiter.consume("scope", "first", 5, 60)
    clock.advance(1)
    limiter.consume("scope", "second", 5, 60)
    clock.advance(1)
    limiter.consume("scope", "first", 5, 60)
    limiter.consume("scope", "third", 5, 60)

    assert limiter.bucket_count == 2
    assert limiter.reset("scope", "second") is False
    assert limiter.reset("scope", "first") is True
    assert limiter.reset("scope", "third") is True


def test_capacity_cleanup_prefers_expired_buckets_before_lru_eviction():
    clock = FakeClock()
    limiter = AuthRateLimiter(AuthRateLimitConfig(max_buckets=2), clock=clock)
    limiter.consume("scope", "short", 1, 1)
    limiter.consume("scope", "long", 1, 100)
    clock.advance(2)

    limiter.consume("scope", "new", 1, 100)

    assert limiter.bucket_count == 2
    assert limiter.reset("scope", "short") is False
    assert limiter.reset("scope", "long") is True
    assert limiter.reset("scope", "new") is True


def test_concurrent_consume_cannot_exceed_limit():
    worker_count = 32
    allowed_limit = 7
    barrier = threading.Barrier(worker_count)
    limiter = AuthRateLimiter(AuthRateLimitConfig(), clock=FakeClock())

    def consume_once(_index: int) -> bool:
        barrier.wait()
        return limiter.consume("concurrent", "shared", allowed_limit, 60).allowed

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(consume_once, range(worker_count)))

    assert sum(results) == allowed_limit
    assert len(results) - sum(results) == worker_count - allowed_limit


def test_fingerprints_are_normalized_and_buckets_contain_no_raw_identity_data():
    ipv4 = client_fingerprint("192.0.2.15")
    ipv6 = client_fingerprint("2001:0db8:0:0:0:0:0:1")
    email = account_fingerprint(" User@Example.COM ")
    pair = login_pair_fingerprint(ipv4, email)
    limiter = AuthRateLimiter(AuthRateLimitConfig())
    limiter.consume(LOGIN_CLIENT_ACCOUNT_SCOPE, pair, 1, 60)

    assert len({ipv4, ipv6, email, pair}) == 4
    assert ipv6 == client_fingerprint("2001:db8::1")
    assert email == account_fingerprint("user@example.com")
    assert client_fingerprint(None) == client_fingerprint("")
    assert normalize_client_address(None) == "unknown-client"
    bucket_text = repr(limiter._buckets)
    assert "192.0.2.15" not in bucket_text
    assert "User@Example.COM" not in bucket_text
    assert "user@example.com" not in bucket_text


@pytest.mark.parametrize("bad_limit", [0, -1, True, MAX_LIMIT + 1])
def test_consume_rejects_invalid_limit(bad_limit):
    limiter = AuthRateLimiter(AuthRateLimitConfig())
    with pytest.raises(ValueError):
        limiter.consume("scope", "key", bad_limit, 60)


@pytest.mark.parametrize("bad_window", [0, -1, True, MAX_WINDOW_SECONDS + 1])
def test_consume_rejects_invalid_window(bad_window):
    limiter = AuthRateLimiter(AuthRateLimitConfig())
    with pytest.raises(ValueError):
        limiter.consume("scope", "key", 1, bad_window)
