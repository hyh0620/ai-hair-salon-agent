"""Build and verify publishable, redacted evaluation snapshots."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from eval.evaluation_metrics import (
    compute_metrics,
    first_matching_source_rank,
    normalize_source_identifier,
)


SCHEMA_VERSION = "1.0"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def build_verified_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
    """Create a redacted snapshot only for a complete real evaluation run."""
    _assert_snapshot_eligible(report)
    context = dict(report.get("run_context") or {})
    case_results = [
        _sanitize_case_result(item)
        for item in report.get("case_results") or []
    ]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": report.get("generated_at"),
        "git_commit_sha": context.get("git_commit_sha"),
        "dataset_sha256": context.get("dataset_sha256"),
        "corpus_version": context.get("corpus_version"),
        "collection": context.get("collection"),
        "dataset_count": report.get("dataset_count"),
        "samples_by_category": report.get("samples_by_category") or {},
        "model": context.get("model"),
        "embedding_model": context.get("embedding_model"),
        "evaluation_dates": context.get("evaluation_dates") or {},
        "metrics": report.get("metrics") or {},
        "case_results": case_results,
    }
    verify_snapshot(snapshot)
    return snapshot


def write_verified_snapshot(
    report: Mapping[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    snapshot = build_verified_snapshot(report)
    generated_at = datetime.fromisoformat(
        str(snapshot["generated_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    short_sha = str(snapshot["git_commit_sha"])[:8]
    stem = f"verified_{generated_at:%Y%m%d}_{short_sha}"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_snapshot_markdown(snapshot),
        encoding="utf-8",
    )
    return json_path, markdown_path


def verify_snapshot(snapshot: Mapping[str, Any]) -> None:
    errors: list[str] = []
    cases = list(snapshot.get("case_results") or [])

    if snapshot.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if snapshot.get("dataset_count") != len(cases):
        errors.append("dataset_count does not match case_results")
    if len({item.get("id") for item in cases}) != len(cases):
        errors.append("case ids must be unique")
    if not SHA256_PATTERN.fullmatch(str(snapshot.get("dataset_sha256") or "")):
        errors.append("dataset_sha256 must be a lowercase SHA-256 value")

    expected_categories = dict(
        Counter(str(item.get("category", "unknown")) for item in cases)
    )
    if snapshot.get("samples_by_category") != expected_categories:
        errors.append("samples_by_category does not match case_results")

    for item in cases:
        expected_sources = item.get("expected_sources") or []
        returned_sources = [
            {"source": source}
            for source in item.get("returned_sources") or []
        ]
        expected_rank = first_matching_source_rank(
            returned_sources,
            expected_sources,
        )
        if item.get("first_relevant_rank") != expected_rank:
            errors.append(
                f"{item.get('id')}: first_relevant_rank is inconsistent"
            )

    expected_metrics = compute_metrics(cases)
    if snapshot.get("metrics") != expected_metrics:
        errors.append("metrics do not match recomputed case_results")

    if errors:
        raise ValueError("; ".join(errors))


def render_snapshot_markdown(snapshot: Mapping[str, Any]) -> str:
    metrics = snapshot.get("metrics") or {}
    retrieval = metrics.get("retrieval_quality") or {}
    functional = metrics.get("functional_contract") or {}
    lines = [
        "# Verified Evaluation Snapshot",
        "",
        f"- Schema: `{snapshot.get('schema_version')}`",
        f"- Generated at: `{snapshot.get('generated_at')}`",
        f"- Git commit: `{snapshot.get('git_commit_sha')}`",
        f"- Dataset SHA-256: `{snapshot.get('dataset_sha256')}`",
        f"- Dataset cases: `{snapshot.get('dataset_count')}`",
        f"- Corpus: `{snapshot.get('corpus_version')}`",
        f"- Collection: `{snapshot.get('collection')}`",
        f"- Model: `{snapshot.get('model')}`",
        f"- Embedding model: `{snapshot.get('embedding_model')}`",
        "",
        "## Aggregate Results",
        "",
        f"- Functional contract: `{_format_ratio(functional.get('api_contract_pass'))}`",
        f"- RAG cases: `{retrieval.get('rag_cases_evaluated')}`",
        f"- Hit@1: `{_format_ratio(retrieval.get('hit_at_1'))}`",
        f"- Hit@3: `{_format_ratio(retrieval.get('hit_at_3'))}`",
        f"- MRR: `{retrieval.get('mrr')}`",
        "",
        "## Case Evidence",
        "",
        "| ID | Category | HTTP | Contract | Retrieval | First relevant rank | Latency (ms) |",
        "| --- | --- | ---: | --- | --- | ---: | ---: |",
    ]
    for item in snapshot.get("case_results") or []:
        lines.append(
            "| {id} | {category} | {status} | {contract} | {retrieval} | "
            "{rank} | {latency} |".format(
                id=item.get("id"),
                category=item.get("category"),
                status=item.get("actual_status"),
                contract=item.get("contract_pass"),
                retrieval=item.get("retrieval_quality_pass"),
                rank=item.get("first_relevant_rank"),
                latency=item.get("latency_ms"),
            )
        )
    lines.append("")
    lines.append(
        "This file contains redacted aggregate and per-case evidence. "
        "It excludes prompts, raw model responses, trace data, identities, "
        "credentials, database rows, and local paths."
    )
    lines.append("")
    return "\n".join(lines)


def _assert_snapshot_eligible(report: Mapping[str, Any]) -> None:
    results = list(report.get("case_results") or [])
    context = dict(report.get("run_context") or {})
    reasons: list[str] = []
    if not results:
        reasons.append("case_results are empty")
    if any(item.get("skipped") for item in results):
        reasons.append("one or more cases were skipped")
    if any(not item.get("contract_pass") for item in results):
        reasons.append("one or more functional contracts failed")
    if context.get("model") in {None, "", "not_configured"}:
        reasons.append("model is not configured")
    if context.get("embedding_model") in {None, "", "unknown"}:
        reasons.append("embedding model is not configured")
    if context.get("git_worktree_clean") is not True:
        reasons.append("git worktree is not clean")
    if not context.get("dataset_sha256"):
        reasons.append("dataset SHA-256 is missing")
    retrieval = (report.get("metrics") or {}).get("retrieval_quality") or {}
    if not retrieval.get("rag_cases_evaluated"):
        reasons.append("no source-expected RAG cases were evaluated")
    if reasons:
        raise ValueError(
            "verified snapshot requires a complete real run: "
            + "; ".join(reasons)
        )


def _sanitize_case_result(item: Mapping[str, Any]) -> dict[str, Any]:
    expected_sources = [
        normalize_source_identifier(source)
        for source in (
            item.get("expected_sources")
            or item.get("expected_source")
            or []
        )
    ]
    returned_sources = []
    for source in item.get("returned_sources") or []:
        if isinstance(source, Mapping):
            identifier = normalize_source_identifier(
                source.get("source") or source.get("title")
            )
        else:
            identifier = normalize_source_identifier(source)
        if identifier and identifier not in returned_sources:
            returned_sources.append(identifier)

    return {
        "id": item.get("id"),
        "category": item.get("category"),
        "mode": item.get("mode"),
        "expected_route": item.get("expected_route"),
        "actual_route": item.get("actual_route"),
        "expected_tool_or_service": item.get("expected_tool_or_service"),
        "actual_tool_or_service": item.get("actual_tool_or_service"),
        "expected_status": item.get("expected_http_status"),
        "expected_http_status": item.get("expected_http_status"),
        "actual_status": item.get("http_status"),
        "http_status": item.get("http_status"),
        "expected_business_result": item.get("expected_business_result"),
        "expected_sources": expected_sources,
        "expected_source": expected_sources,
        "returned_sources": returned_sources,
        "first_relevant_rank": item.get("first_relevant_rank"),
        "contract_pass": bool(item.get("contract_pass")),
        "retrieval_quality_pass": item.get("retrieval_quality_pass"),
        "required_slots_present": bool(item.get("required_slots_present")),
        "discovered_tools": list(item.get("discovered_tools") or []),
        "skipped": bool(item.get("skipped")),
        "latency_ms": round(float(item.get("latency_ms", 0)), 2),
        "resolved_datetimes": dict(item.get("resolved_datetimes") or {}),
    }


def _format_ratio(value: Any) -> str:
    if not isinstance(value, Mapping):
        return str(value)
    return (
        f"{value.get('numerator')} / {value.get('denominator')} "
        f"= {value.get('rate')}"
    )
