"""Evaluation metrics derived from inspectable per-case results."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


BOOKING_MODES = {"booking_api", "appointment_modify_api"}
RETRIEVAL_MODES = {"rag_api", "llm_unconfigured_api", "mcp_direct"}
CONSULTATION_MODES = {
    "rag_api",
    "llm_unconfigured_api",
    "mcp_unavailable_api",
}


def normalize_source_identifier(value: Any) -> str:
    """Return a stable public filename for source-level comparisons."""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    return PurePosixPath(text).name.casefold()


def first_matching_source_rank(
    sources: Sequence[Mapping[str, Any]],
    expected_sources: Sequence[str],
) -> int | None:
    """Rank sources after deduplicating chunks from the same document."""
    expected = {
        normalize_source_identifier(item)
        for item in expected_sources
        if normalize_source_identifier(item)
    }
    if not expected:
        return None

    seen: set[str] = set()
    source_rank = 0
    for source in sources:
        identifier = normalize_source_identifier(
            source.get("source") or source.get("title")
        )
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        source_rank += 1
        if identifier in expected:
            return source_rank
    return None


def compute_metrics(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    evaluated = [item for item in results if not item.get("skipped")]
    booking_cases = [
        item for item in results if item.get("mode") in BOOKING_MODES
    ]
    route_cases = [
        item for item in evaluated if item.get("category") == "routing"
    ]

    expected_success = [
        item for item in booking_cases
        if item.get("expected_http_status") == 200
    ]
    expected_conflict = [
        item for item in booking_cases
        if item.get("expected_http_status") == 409
    ]
    expected_invalid = [
        item for item in booking_cases
        if item.get("expected_http_status") in {400, 422}
    ]
    expected_required_slots = [
        item for item in expected_success
        if item.get("required_slots_present")
    ]
    unnecessary_tool_cases = [
        item for item in route_cases
        if item.get("expected_tool_or_service") != "mcp_rag"
    ]
    source_expected = [
        item for item in evaluated
        if (item.get("expected_sources") or item.get("expected_source"))
        and item.get("mode") in RETRIEVAL_MODES
    ]
    no_result_cases = [
        item for item in evaluated
        if item.get("expected_business_result") == "no_result_handled"
    ]
    mcp_unavailable = [
        item for item in evaluated
        if item.get("mode") == "mcp_unavailable_api"
    ]
    api_error_contract = [
        item for item in evaluated
        if item.get("expected_http_status") in {400, 409, 422, 503}
    ]
    consultation_latencies = [
        float(item.get("latency_ms", 0))
        for item in evaluated
        if item.get("mode") in CONSULTATION_MODES
    ]
    booking_latencies = [
        float(item.get("latency_ms", 0))
        for item in evaluated
        if item.get("mode") in BOOKING_MODES
    ]

    hit1_n = sum(
        1 for item in source_expected
        if item.get("first_relevant_rank") == 1
    )
    hit3_n = sum(
        1
        for item in source_expected
        if item.get("first_relevant_rank") is not None
        and item["first_relevant_rank"] <= 3
    )
    source_match_n = sum(
        1 for item in source_expected
        if item.get("first_relevant_rank") is not None
    )
    citation_presence_n = sum(
        1 for item in source_expected
        if len(item.get("returned_sources") or []) > 0
    )
    reciprocal_sum = sum(
        (1 / item["first_relevant_rank"])
        if item.get("first_relevant_rank")
        else 0
        for item in source_expected
    )

    return {
        "functional_contract": {
            "api_contract_pass": ratio(
                sum(1 for item in evaluated if item.get("contract_pass")),
                len(evaluated),
            ),
            "booking_contract_pass": ratio(
                sum(1 for item in booking_cases if item.get("contract_pass")),
                len(booking_cases),
            ),
            "booking_conflict_contract_pass": ratio(
                sum(1 for item in expected_conflict if item.get("contract_pass")),
                len(expected_conflict),
            ),
            "mcp_unavailable_contract_pass": ratio(
                sum(1 for item in mcp_unavailable if item.get("contract_pass")),
                len(mcp_unavailable),
            ),
            "api_error_contract_pass": ratio(
                sum(1 for item in api_error_contract if item.get("contract_pass")),
                len(api_error_contract),
            ),
        },
        "booking": {
            "booking_success_rate": ratio(
                sum(1 for item in expected_success if item.get("contract_pass")),
                len(expected_success),
            ),
            "conflict_block_rate": ratio(
                sum(1 for item in expected_conflict if item.get("contract_pass")),
                len(expected_conflict),
            ),
            "invalid_booking_rejection_rate": ratio(
                sum(1 for item in expected_invalid if item.get("contract_pass")),
                len(expected_invalid),
            ),
            "required_slot_completion_rate": ratio(
                sum(
                    1 for item in expected_required_slots
                    if item.get("contract_pass")
                ),
                len(expected_required_slots),
            ),
        },
        "routing": {
            "route_accuracy": ratio(
                sum(
                    1 for item in route_cases
                    if item.get("actual_route") == item.get("expected_route")
                ),
                len(route_cases),
            ),
            "tool_or_service_selection_accuracy": ratio(
                sum(
                    1 for item in route_cases
                    if item.get("actual_tool_or_service")
                    == item.get("expected_tool_or_service")
                ),
                len(route_cases),
            ),
            "unnecessary_tool_call_rate": ratio(
                sum(
                    1 for item in unnecessary_tool_cases
                    if item.get("actual_tool_or_service") == "mcp_rag"
                ),
                len(unnecessary_tool_cases),
            ),
        },
        "retrieval_quality": {
            "rag_cases_evaluated": len(source_expected),
            "hit_at_1": ratio(hit1_n, len(source_expected)),
            "hit_at_3": ratio(hit3_n, len(source_expected)),
            "mrr": (
                round(reciprocal_sum / len(source_expected), 4)
                if source_expected
                else None
            ),
            "mrr_numerator_reciprocal_sum": round(reciprocal_sum, 4),
            "mrr_denominator": len(source_expected),
            "citation_presence_rate": ratio(
                citation_presence_n,
                len(source_expected),
            ),
            "citation_expected_source_match_rate": ratio(
                source_match_n,
                len(source_expected),
            ),
            "empty_result_handling_correctness": ratio(
                sum(
                    1 for item in no_result_cases
                    if item.get("contract_pass")
                    and not item.get("returned_sources")
                ),
                len(no_result_cases),
            ),
        },
        "stability": {
            "mcp_unavailable_handling_correctness": ratio(
                sum(1 for item in mcp_unavailable if item.get("contract_pass")),
                len(mcp_unavailable),
            ),
            "api_error_contract_correctness": ratio(
                sum(1 for item in api_error_contract if item.get("contract_pass")),
                len(api_error_contract),
            ),
            "rag_server_tool_discovery_success": any(
                "query_knowledge_hub" in (item.get("discovered_tools") or [])
                for item in evaluated
            ),
        },
        "latency": {
            "consultation_api_p50_ms": percentile(
                consultation_latencies,
                50,
            ),
            "consultation_api_p95_ms": percentile(
                consultation_latencies,
                95,
            ),
            "consultation_api_samples": len(consultation_latencies),
            "booking_api_p50_ms": percentile(booking_latencies, 50),
            "booking_api_p95_ms": percentile(booking_latencies, 95),
            "booking_api_samples": len(booking_latencies),
        },
    }


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def percentile(values: Sequence[float], percentile_value: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    index = round((percentile_value / 100) * (len(ordered) - 1))
    return round(ordered[index], 2)
