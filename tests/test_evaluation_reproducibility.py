import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from eval.dataset_resolver import resolve_evaluation_cases
from eval.evaluation_metrics import (
    compute_metrics,
    first_matching_source_rank,
)
from eval.run_evaluation import (
    evaluate_appointment_modify_case,
    load_cases,
)
from eval.verified_snapshot import (
    build_verified_snapshot,
    verify_snapshot,
)


DATASET = Path(__file__).resolve().parents[1] / "eval" / "golden_dataset.jsonl"


def test_evaluation_dates_are_future_weekdays_and_stable_for_one_run():
    now = datetime(2026, 7, 24, 23, 59)
    cases = [{
        "id": "date",
        "request_json": {
            "start_time": "{{EVAL_DATE_DAY_1}} 14:00",
        },
        "setup_request_json": {
            "start_time": "{{EVAL_DATE_DAY_1}} 14:00",
        },
    }]

    first = resolve_evaluation_cases(cases, now=now)
    second = resolve_evaluation_cases(cases, now=now)

    assert first.evaluation_base_date > now.date()
    assert first.evaluation_base_date.weekday() < 5
    assert first.resolved_dates == second.resolved_dates
    resolved = first.cases[0]
    assert (
        resolved["request_json"]["start_time"]
        == resolved["setup_request_json"]["start_time"]
    )


def test_dataset_conflict_setup_and_request_resolve_to_same_time():
    cases = load_cases(DATASET)
    resolved = resolve_evaluation_cases(
        cases,
        evaluation_base_date="2026-08-03",
    )
    conflict = next(case for case in resolved.cases if case["id"] == "B007")

    assert (
        conflict["setup_request_json"]["start_time"]
        == conflict["request_json"]["start_time"]
        == "2026-08-03 18:00"
    )


def test_dataset_resolver_leaves_non_date_text_unchanged():
    cases = [{"id": "plain", "input": "染发后怎么护理？"}]

    resolved = resolve_evaluation_cases(
        cases,
        evaluation_base_date="2026-08-03",
    )

    assert resolved.cases[0]["input"] == "染发后怎么护理？"


def test_dataset_resolver_rejects_unknown_placeholder():
    with pytest.raises(
        ValueError,
        match="unknown evaluation placeholder: EVAL_DATE_UNKNOWN",
    ):
        resolve_evaluation_cases(
            [{"id": "bad", "input": "{{EVAL_DATE_UNKNOWN}}"}],
            evaluation_base_date="2026-08-03",
        )


def test_source_rank_deduplicates_chunks_from_the_same_document():
    sources = [
        {"source": "/runtime/other.pdf"},
        {"source": "/runtime/other.pdf"},
        {"source": "/runtime/expected.pdf"},
    ]

    assert first_matching_source_rank(sources, ["expected.pdf"]) == 2


def test_metrics_exclude_skipped_source_cases_and_recompute_mrr():
    evaluated = _result(
        id="R001",
        mode="rag_api",
        expected_sources=["expected.pdf"],
        returned_sources=[{"source": "other.pdf"}, {"source": "expected.pdf"}],
        first_relevant_rank=2,
        retrieval_quality_pass=True,
    )
    skipped = _result(
        id="R002",
        mode="rag_api",
        expected_sources=["missing.pdf"],
        returned_sources=[],
        first_relevant_rank=None,
        retrieval_quality_pass=None,
        skipped=True,
        contract_pass=False,
    )

    metrics = compute_metrics([evaluated, skipped])

    assert metrics["retrieval_quality"]["hit_at_1"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0.0,
    }
    assert metrics["retrieval_quality"]["hit_at_3"]["rate"] == 1.0
    assert metrics["retrieval_quality"]["mrr"] == 0.5
    assert metrics["functional_contract"]["api_contract_pass"]["denominator"] == 1


class _FakeLifecycleClient:
    def __init__(self):
        self.calls = []

    def request(
        self,
        method,
        path,
        json_body=None,
        params=None,
        trace_id=None,
    ):
        self.calls.append((method, path, json_body, params, trace_id))
        if method == "POST":
            return _http_response(
                200,
                {
                    "data": {
                        "appointment_id": 123,
                        "stylist_id": 1,
                        "start_time": "2026-08-03 17:00",
                    }
                },
            )
        if method == "GET" and path == "/api/appointment/123":
            return _http_response(
                200,
                {
                    "data": {
                        "appointment": {
                            "appointment_id": 123,
                            "stylist_id": 1,
                            "start_time": "2026-08-03T17:00:00",
                            "version": 1,
                        }
                    }
                },
            )
        if method == "PATCH":
            return _http_response(
                200,
                {
                    "data": {
                        "status": "success",
                        "appointment": {
                            "appointment_id": 123,
                            "stylist_id": 1,
                            "start_time": "2026-08-04T15:00:00",
                            "version": 2,
                        },
                    }
                },
            )
        if params == {"date": "2026-08-03"}:
            return _http_response(200, [])
        if params == {"date": "2026-08-04"}:
            return _http_response(
                200,
                [{
                    "appointment_id": 123,
                    "status": "busy",
                    "start_time": "15:00",
                }],
            )
        raise AssertionError(f"unexpected request: {method} {path} {params}")


def test_b005_uses_official_patch_api_and_verifies_schedule_migration():
    case = {
        "id": "B005",
        "category": "booking",
        "evaluation_mode": "appointment_modify_api",
        "expected_route": "appointment",
        "expected_tool_or_service": "appointment_service",
        "expected_http_status": 200,
        "expected_retrieval_mode": "not_applicable",
        "expected_sources": [],
        "expected_business_result": "appointment_modified_atomically",
        "setup_request_json": {
            "user_id": "eval_B005",
            "project": "男士短发",
            "start_time": "2026-08-03 17:00",
            "duration": "45分钟",
            "stylist_id": 1,
        },
        "update_request_json": {
            "target_date": "2026-08-04",
            "start_time": "15:00",
        },
    }
    client = _FakeLifecycleClient()

    result = evaluate_appointment_modify_case(
        client,
        case,
        _context(),
    )

    assert result["contract_pass"] is True
    assert result["actual_business_result"]["original_version"] == 1
    assert result["actual_business_result"]["updated_version"] == 2
    assert result["actual_business_result"]["old_schedule_released"] is True
    assert result["actual_business_result"]["new_schedule_busy"] is True
    assert any(
        method == "PATCH" and path == "/api/appointment/123"
        for method, path, *_ in client.calls
    )


def test_verified_snapshot_recomputes_case_level_metrics():
    cases = [
        _result(id="B001", mode="booking_api"),
        _result(
            id="R001",
            mode="rag_api",
            category="rag",
            expected_route="consultation",
            actual_route="consultation",
            expected_tool_or_service="mcp_rag",
            actual_tool_or_service="mcp_rag",
            expected_sources=["expected.pdf"],
            returned_sources=[{"source": "/private/expected.pdf"}],
            first_relevant_rank=1,
            retrieval_quality_pass=True,
        ),
    ]
    report = {
        "generated_at": "2026-08-01T00:00:00+00:00",
        "dataset_count": 2,
        "samples_by_category": {"booking": 1, "rag": 1},
        "run_context": {
            "git_commit_sha": "a" * 40,
            "git_worktree_clean": True,
            "dataset_sha256": "b" * 64,
            "corpus_version": "salon_knowledge@2026.07",
            "collection": "salon_knowledge",
            "model": "openai-compatible:qwen-plus",
            "embedding_model": "text-embedding-v4",
            "evaluation_dates": {
                "resolved_dates": {
                    "EVAL_DATE_DAY_1": "2026-08-03",
                }
            },
        },
        "case_results": cases,
        "metrics": compute_metrics(cases),
    }

    snapshot = build_verified_snapshot(report)

    verify_snapshot(snapshot)
    assert snapshot["case_results"][1]["returned_sources"] == ["expected.pdf"]
    tampered = copy.deepcopy(snapshot)
    tampered["metrics"]["retrieval_quality"]["hit_at_1"]["denominator"] = 2
    with pytest.raises(ValueError, match="metrics do not match"):
        verify_snapshot(tampered)


def test_verified_snapshot_rejects_incomplete_or_mock_run():
    case = _result(
        id="R001",
        mode="rag_api",
        category="rag",
        expected_sources=["expected.pdf"],
        skipped=True,
        contract_pass=False,
    )
    report = {
        "generated_at": "2026-08-01T00:00:00+00:00",
        "dataset_count": 1,
        "samples_by_category": {"rag": 1},
        "run_context": {
            "git_commit_sha": "a" * 40,
            "git_worktree_clean": True,
            "dataset_sha256": "b" * 64,
            "corpus_version": "salon_knowledge@2026.07",
            "collection": "salon_knowledge",
            "model": "not_configured",
            "embedding_model": "unknown",
        },
        "case_results": [case],
        "metrics": compute_metrics([case]),
    }

    with pytest.raises(ValueError, match="complete real run"):
        build_verified_snapshot(report)


def test_committed_verified_snapshot_recomputes_successfully():
    snapshot_path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "snapshots"
        / "verified_20260724_4bbe6d6d.json"
    )

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))

    verify_snapshot(payload)
    assert payload["dataset_count"] == 28
    assert payload["metrics"]["functional_contract"]["api_contract_pass"] == {
        "numerator": 28,
        "denominator": 28,
        "rate": 1.0,
    }


def _result(
    *,
    id,
    mode,
    category="booking",
    expected_route="appointment",
    actual_route="appointment",
    expected_tool_or_service="appointment_service",
    actual_tool_or_service="appointment_service",
    expected_http_status=200,
    expected_sources=None,
    returned_sources=None,
    first_relevant_rank=None,
    contract_pass=True,
    retrieval_quality_pass=None,
    skipped=False,
):
    return {
        "id": id,
        "category": category,
        "mode": mode,
        "expected_route": expected_route,
        "actual_route": actual_route,
        "expected_tool_or_service": expected_tool_or_service,
        "actual_tool_or_service": actual_tool_or_service,
        "expected_http_status": expected_http_status,
        "expected_sources": expected_sources or [],
        "expected_source": expected_sources or [],
        "returned_sources": returned_sources or [],
        "first_relevant_rank": first_relevant_rank,
        "expected_business_result": "booking_confirmed",
        "contract_pass": contract_pass,
        "retrieval_quality_pass": retrieval_quality_pass,
        "required_slots_present": mode in {
            "booking_api",
            "appointment_modify_api",
        },
        "discovered_tools": [],
        "skipped": skipped,
        "latency_ms": 10.0,
        "resolved_datetimes": {},
    }


def _http_response(status, body):
    return {
        "status": status,
        "body": body,
        "latency_ms": 10.0,
        "trace_id": "trace",
    }


def _context():
    return {
        "run_timestamp": "2026-08-01T00:00:00+00:00",
        "corpus_version": "salon_knowledge@2026.07",
        "model": "model",
        "embedding_model": "embedding",
        "git_commit_sha": "a" * 40,
    }
