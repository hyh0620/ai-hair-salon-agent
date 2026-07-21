"""Deterministic appointment status labels for customer-facing presentation."""

from __future__ import annotations

from typing import Any


APPOINTMENT_STATUS_LABELS = {
    "confirmed": "已确认",
    "cancelled": "已取消",
    "completed": "已完成",
}


def appointment_status_label(status: Any) -> str:
    """Return a customer-facing label without changing the stored status code."""
    value = getattr(status, "value", status)
    normalized = str(value or "").strip().lower()
    return APPOINTMENT_STATUS_LABELS.get(normalized, "未知状态")
