"""Explicit policy for real external provider calls.

Normal local runtime remains backward compatible: an unset policy allows the
configured providers. Tests, CI, and isolated validation set the policy to
``deny`` before importing application modules.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any


class ExternalCallPolicy(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class ExternalCallPolicyError(RuntimeError):
    """Raised when EXTERNAL_CALL_POLICY has an unsupported value."""


class ExternalCallBlockedError(RuntimeError):
    """Safe error raised before a denied provider can leave the process."""

    def __init__(self, provider: str, location: str):
        self.provider = _safe_label(provider)
        self.location = _safe_label(location)
        self.policy = ExternalCallPolicy.DENY.value
        super().__init__(
            "External provider call blocked: "
            f"provider={self.provider} policy={self.policy} location={self.location}"
        )


def get_external_call_policy() -> ExternalCallPolicy:
    """Return the explicit policy; an unset variable preserves normal runtime."""
    raw_value = os.getenv("EXTERNAL_CALL_POLICY")
    if raw_value is None:
        return ExternalCallPolicy.ALLOW

    normalized = raw_value.strip().lower()
    try:
        return ExternalCallPolicy(normalized)
    except ValueError as exc:
        raise ExternalCallPolicyError(
            "Invalid EXTERNAL_CALL_POLICY; expected 'allow' or 'deny'."
        ) from exc


def assert_external_call_allowed(provider: str, location: str) -> None:
    """Reject a real provider before client creation, I/O, or subprocess start."""
    if get_external_call_policy() is ExternalCallPolicy.DENY:
        raise ExternalCallBlockedError(provider, location)


def load_runtime_dotenv(
    dotenv_path: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> bool:
    """Load local runtime configuration only when external calls are allowed.

    Hermetic processes populate all required test settings explicitly and must
    not read the developer's private project ``.env`` file.
    """
    if get_external_call_policy() is ExternalCallPolicy.DENY:
        return False

    from dotenv import load_dotenv

    path = Path(dotenv_path) if dotenv_path is not None else None
    return bool(load_dotenv(dotenv_path=path, **kwargs))


def _safe_label(value: str, limit: int = 96) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "unknown"))
    return normalized[:limit] or "unknown"
