import json
from pathlib import Path

from eval.report_generator import CSV_FIELDS, generate_markdown


DATASET = Path(__file__).resolve().parents[1] / "eval" / "golden_dataset.jsonl"
REQUIRED_FIELDS = {
    "id",
    "category",
    "input",
    "expected_route",
    "expected_tool_or_service",
    "expected_http_status",
    "expected_retrieval_mode",
    "expected_sources",
    "expected_business_result",
    "notes",
}


def load_cases():
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_golden_dataset_shape_and_counts():
    cases = load_cases()

    assert len(cases) == 28
    assert len({case["id"] for case in cases}) == len(cases)

    counts = {}
    for case in cases:
        assert REQUIRED_FIELDS.issubset(case.keys())
        assert isinstance(case["expected_sources"], list)
        assert isinstance(case["expected_http_status"], int)
        counts[case["category"]] = counts.get(case["category"], 0) + 1

    assert counts["booking"] >= 8
    assert counts["rag"] >= 8
    assert 4 <= counts["routing"] <= 6
    assert 4 <= counts["exception"] <= 6


def test_golden_dataset_modes_are_known():
    known_modes = {
        "booking_api",
        "appointment_modify_api",
        "rag_api",
        "service_catalog",
        "task_classifier",
        "mcp_direct",
        "mcp_unavailable_api",
        "llm_unconfigured_api",
    }

    for case in load_cases():
        assert case["evaluation_mode"] in known_modes


def test_golden_dataset_uses_dynamic_dates_and_real_modify_mode():
    content = DATASET.read_text(encoding="utf-8")
    cases = {case["id"]: case for case in load_cases()}

    assert "2026-07-10" not in content
    assert "2026-07-11" not in content
    assert "{{EVAL_DATE_DAY_1}}" in content
    assert "{{EVAL_DATE_DAY_2}}" in content
    assert cases["B005"]["evaluation_mode"] == "appointment_modify_api"
    assert cases["B005"]["expected_business_result"] == (
        "appointment_modified_atomically"
    )
    assert "update_request_json" in cases["B005"]


def test_report_generator_includes_metrics_sections():
    report = {
        "generated_at": "2026-07-06T00:00:00Z",
        "dataset_count": 1,
        "samples_by_category": {"booking": 1},
        "run_context": {
            "git_commit_sha": "abc123",
            "corpus_version": "salon_knowledge@2026.07",
            "model": "not_configured",
            "embedding_model": "text-embedding-v4",
        },
        "metrics": {
            "functional_contract": {"api_contract_pass": {"numerator": 1, "denominator": 1, "rate": 1.0}},
            "booking": {"booking_success_rate": {"numerator": 1, "denominator": 1, "rate": 1.0}},
            "routing": {},
            "retrieval_quality": {"hit_at_1": {"numerator": 0, "denominator": 1, "rate": 0.0}},
            "stability": {},
            "latency": {},
        },
        "case_results": [],
    }

    markdown = generate_markdown(report)

    assert "Functional Contract Results" in markdown
    assert "Retrieval Quality Results" in markdown
    assert "booking_success_rate" in markdown
    assert "0 / 1 = 0.0" in markdown


def test_evaluation_result_csv_contract_fields_are_present():
    required_fields = {
        "id",
        "category",
        "input",
        "expected_route",
        "actual_route",
        "expected_source",
        "expected_sources",
        "returned_sources",
        "first_relevant_rank",
        "contract_pass",
        "retrieval_quality_pass",
        "latency_ms",
        "trace_id",
        "error",
        "resolved_datetimes",
        "run_timestamp",
        "corpus_version",
        "model",
        "embedding_model",
        "git_commit_sha",
    }

    assert required_fields.issubset(set(CSV_FIELDS))
