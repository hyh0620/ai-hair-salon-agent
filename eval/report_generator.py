"""Generate human-readable and inspectable evaluation reports."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


CSV_FIELDS = [
    "id",
    "category",
    "mode",
    "input",
    "expected_route",
    "actual_route",
    "expected_tool_or_service",
    "actual_tool_or_service",
    "expected_http_status",
    "http_status",
    "expected_retrieval_mode",
    "actual_retrieval_mode",
    "expected_source",
    "returned_sources",
    "first_relevant_rank",
    "contract_pass",
    "retrieval_quality_pass",
    "latency_ms",
    "trace_id",
    "error",
    "run_timestamp",
    "corpus_version",
    "model",
    "embedding_model",
    "git_commit_sha",
]


def generate_markdown(report: Dict[str, Any]) -> str:
    metrics = report.get("metrics", {})
    lines: List[str] = []
    lines.append("# Salon AI Agent Evaluation Report")
    lines.append("")
    lines.append(f"Generated at: `{report.get('generated_at')}`")
    lines.append(f"Dataset cases: `{report.get('dataset_count')}`")
    lines.append("")

    context = report.get("run_context") or {}
    if context:
        lines.append("## Run Context")
        for key in ("git_commit_sha", "corpus_version", "model", "embedding_model"):
            lines.append(f"- `{key}`: {context.get(key)}")
        lines.append("")

    lines.append("## Sample Counts")
    for category, count in sorted((report.get("samples_by_category") or {}).items()):
        lines.append(f"- `{category}`: {count}")
    lines.append("")

    lines.append("## Functional Contract Results")
    _append_section(lines, metrics.get("functional_contract") or {})
    lines.append("")

    lines.append("## Booking Business Results")
    _append_section(lines, metrics.get("booking") or {})
    lines.append("")

    lines.append("## Routing Results")
    _append_section(lines, metrics.get("routing") or {})
    lines.append("")

    lines.append("## Retrieval Quality Results")
    _append_section(lines, metrics.get("retrieval_quality") or {})
    lines.append("")

    lines.append("## Stability Results")
    _append_section(lines, metrics.get("stability") or {})
    lines.append("")

    lines.append("## Latency")
    _append_section(lines, metrics.get("latency") or {})
    lines.append("")

    lines.append("## Retrieval Case Ranks")
    retrieval_rows = [
        item for item in report.get("case_results", [])
        if item.get("expected_source")
    ]
    if not retrieval_rows:
        lines.append("- No source-expected retrieval cases.")
    else:
        for item in retrieval_rows:
            returned = ", ".join(_source_name(source) for source in item.get("returned_sources", [])[:3])
            lines.append(
                f"- `{item.get('id')}` expected `{item.get('expected_source')}` "
                f"first_relevant_rank=`{item.get('first_relevant_rank')}` "
                f"contract_pass=`{item.get('contract_pass')}` "
                f"retrieval_quality_pass=`{item.get('retrieval_quality_pass')}` "
                f"returned=`{returned}`"
            )
    lines.append("")

    lines.append("## Contract Failed Cases")
    failed = [
        item for item in report.get("case_results", [])
        if not item.get("contract_pass") and not item.get("skipped")
    ]
    if not failed:
        lines.append("No functional contract failures.")
    else:
        for item in failed:
            lines.append(
                f"- `{item.get('id')}` expected status `{item.get('expected_http_status')}` "
                f"actual status `{item.get('http_status')}` error: {item.get('error', '')}"
            )
    lines.append("")

    retrieval_misses = [
        item for item in report.get("case_results", [])
        if item.get("retrieval_quality_pass") is False
    ]
    lines.append("## Expected Source Misses")
    if not retrieval_misses:
        lines.append("No evaluated source-expected case completely missed its expected source. Hit@1 misses are still reflected in the rank table and retrieval metrics above.")
    else:
        for item in retrieval_misses:
            lines.append(
                f"- `{item.get('id')}` expected source `{item.get('expected_source')}` "
                f"returned `{item.get('returned_sources')}`"
            )
    lines.append("")

    skipped = [item for item in report.get("case_results", []) if item.get("skipped")]
    if skipped:
        lines.append("## Skipped Cases")
        for item in skipped:
            lines.append(f"- `{item.get('id')}`: {item.get('error')}")
        lines.append("")

    return "\n".join(lines)


def write_reports(
    report: Dict[str, Any],
    json_path: Path,
    markdown_path: Path,
    results_json_path: Path,
    results_csv_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    results_json_path.parent.mkdir(parents=True, exist_ok=True)
    results_csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(generate_markdown(report), encoding="utf-8")
    results_json_path.write_text(
        json.dumps(
            {
                "generated_at": report.get("generated_at"),
                "run_context": report.get("run_context") or {},
                "case_results": report.get("case_results") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_results_csv(report.get("case_results") or [], results_csv_path)


def count_by_category(cases: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    return dict(Counter(str(case.get("category", "unknown")) for case in cases))


def _append_section(lines: List[str], section_metrics: Dict[str, Any]) -> None:
    if not section_metrics:
        lines.append("- No metrics recorded.")
        return
    for key, value in section_metrics.items():
        lines.append(f"- `{key}`: {_format_metric(value)}")


def _format_metric(value: Any) -> str:
    if isinstance(value, dict) and {"numerator", "denominator", "rate"}.issubset(value.keys()):
        return f"{value['numerator']} / {value['denominator']} = {value['rate']}"
    return str(value)


def _source_name(source: Dict[str, Any]) -> str:
    return str(source.get("source") or source.get("title") or "")


def _write_results_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                field: _csv_value(row.get(field))
                for field in CSV_FIELDS
            })


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return value
