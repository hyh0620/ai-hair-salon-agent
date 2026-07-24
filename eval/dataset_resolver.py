"""Resolve stable, future dates for one evaluation run."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from config.time_config import time_config


PLACEHOLDER_PATTERN = re.compile(r"\{\{(EVAL_[A-Z0-9_]+)\}\}")
KNOWN_DATE_PLACEHOLDERS = (
    "EVAL_DATE_DAY_1",
    "EVAL_DATE_DAY_2",
)


@dataclass(frozen=True)
class ResolvedEvaluationDataset:
    cases: list[dict[str, Any]]
    evaluation_base_date: date
    resolved_dates: dict[str, str]

    def context(self) -> dict[str, Any]:
        return {
            "evaluation_base_date": self.evaluation_base_date.isoformat(),
            "resolved_dates": dict(self.resolved_dates),
            "timezone": "Asia/Shanghai",
        }


def resolve_evaluation_cases(
    cases: Sequence[Mapping[str, Any]],
    *,
    evaluation_base_date: date | str | None = None,
    now: datetime | None = None,
) -> ResolvedEvaluationDataset:
    """Resolve every dataset placeholder from one immutable date mapping."""
    reference_now = now or time_config.now()
    base_date = _resolve_base_date(evaluation_base_date, reference_now)
    day_two = _next_weekday(base_date + timedelta(days=1))
    replacements = {
        "EVAL_DATE_DAY_1": base_date.isoformat(),
        "EVAL_DATE_DAY_2": day_two.isoformat(),
    }

    resolved_cases: list[dict[str, Any]] = []
    for original in cases:
        resolved = _replace_placeholders(copy.deepcopy(dict(original)), replacements)
        resolved["_resolved_datetimes"] = _collect_resolved_datetimes(resolved)
        resolved_cases.append(resolved)

    return ResolvedEvaluationDataset(
        cases=resolved_cases,
        evaluation_base_date=base_date,
        resolved_dates=replacements,
    )


def _resolve_base_date(value: date | str | None, reference_now: datetime) -> date:
    if isinstance(value, str):
        try:
            candidate = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "evaluation base date must use YYYY-MM-DD"
            ) from exc
    elif isinstance(value, date):
        candidate = value
    elif value is None:
        candidate = _next_weekday(reference_now.date() + timedelta(days=14))
    else:
        raise TypeError("evaluation base date must be a date, string, or None")

    if candidate <= reference_now.date():
        raise ValueError("evaluation base date must be in the future")
    if candidate.weekday() >= 5:
        raise ValueError("evaluation base date must be a weekday")
    return candidate


def _next_weekday(candidate: date) -> date:
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _replace_placeholders(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if not isinstance(value, str):
        return value

    unknown = {
        token
        for token in PLACEHOLDER_PATTERN.findall(value)
        if token not in replacements
    }
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown evaluation placeholder: {names}")

    return PLACEHOLDER_PATTERN.sub(
        lambda match: replacements[match.group(1)],
        value,
    )


def _collect_resolved_datetimes(case: Mapping[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for section_name in (
        "setup_request_json",
        "request_json",
        "update_request_json",
    ):
        section = case.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for field_name in ("start_time", "target_date"):
            value = section.get(field_name)
            if value:
                resolved[f"{section_name}.{field_name}"] = str(value)
    return resolved
