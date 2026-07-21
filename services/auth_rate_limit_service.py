"""Thread-safe, bounded sliding-window limits for authentication endpoints."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import ipaddress
import math
import threading
import time
from typing import Callable, Deque, Optional

from config.auth_rate_limit_config import (
    MAX_LIMIT,
    MAX_WINDOW_SECONDS,
    AuthRateLimitConfig,
)
from services.auth_service import AuthService, AuthServiceError


LOGIN_CLIENT_SCOPE = "login_client"
LOGIN_CLIENT_ACCOUNT_SCOPE = "login_client_account"
REGISTER_CLIENT_SCOPE = "register_client"
UNKNOWN_CLIENT = "unknown-client"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass
class _SlidingWindowBucket:
    timestamps: Deque[float]
    window_seconds: int
    last_seen: float


class AuthRateLimiter:
    """In-process limiter whose state is shared by one FastAPI application."""

    def __init__(
        self,
        config: Optional[AuthRateLimitConfig] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or AuthRateLimitConfig.from_env()
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[
            tuple[str, str],
            _SlidingWindowBucket,
        ] = OrderedDict()
        self._last_cleanup = self._now()

    def consume(
        self,
        scope: str,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        if not scope or not key:
            raise ValueError("scope and key are required")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= MAX_WINDOW_SECONDS
        ):
            raise ValueError(
                f"window_seconds must be between 1 and {MAX_WINDOW_SECONDS}"
            )

        now = self._now()
        bucket_key = (scope, key)
        with self._lock:
            if now - self._last_cleanup >= self.config.cleanup_interval_seconds:
                self._prune_locked(now)

            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                self._make_capacity_locked(now)
                bucket = _SlidingWindowBucket(deque(), window_seconds, now)
                self._buckets[bucket_key] = bucket
            else:
                bucket.window_seconds = window_seconds

            self._expire_bucket(bucket, now)
            bucket.last_seen = now
            self._buckets.move_to_end(bucket_key)

            if len(bucket.timestamps) >= limit:
                retry_after = max(
                    1,
                    math.ceil(bucket.timestamps[0] + window_seconds - now),
                )
                return RateLimitDecision(False, retry_after)

            bucket.timestamps.append(now)
            return RateLimitDecision(True, 0)

    def reset(self, scope: str, key: str) -> bool:
        with self._lock:
            return self._buckets.pop((scope, key), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._last_cleanup = self._now()

    def prune(self) -> int:
        now = self._now()
        with self._lock:
            return self._prune_locked(now)

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("rate limiter clock must return a finite value")
        return now

    def _make_capacity_locked(self, now: float) -> None:
        if len(self._buckets) < self.config.max_buckets:
            return
        self._prune_locked(now)
        while len(self._buckets) >= self.config.max_buckets:
            self._buckets.popitem(last=False)

    def _prune_locked(self, now: float) -> int:
        removed = 0
        for bucket_key, bucket in list(self._buckets.items()):
            self._expire_bucket(bucket, now)
            if not bucket.timestamps:
                del self._buckets[bucket_key]
                removed += 1
        self._last_cleanup = now
        return removed

    @staticmethod
    def _expire_bucket(bucket: _SlidingWindowBucket, now: float) -> None:
        cutoff = now - bucket.window_seconds
        while bucket.timestamps and bucket.timestamps[0] <= cutoff:
            bucket.timestamps.popleft()


def normalize_client_address(value: Optional[str]) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return UNKNOWN_CLIENT
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        return candidate.casefold() or UNKNOWN_CLIENT


def client_fingerprint(value: Optional[str]) -> str:
    return _fingerprint("client", normalize_client_address(value))


def account_fingerprint(value: str) -> str:
    try:
        normalized = AuthService.normalize_email(value)
    except AuthServiceError:
        normalized = "invalid-email"
    return _fingerprint("account", normalized)


def login_pair_fingerprint(client_key: str, account_key: str) -> str:
    return _fingerprint("login-pair", f"{client_key}:{account_key}")


def _fingerprint(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).hexdigest()
