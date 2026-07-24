"""Run reproducible evaluation against the salon AI agent."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.task_classification.task_classifier import TaskClassifier
from config.external_calls import assert_external_call_allowed, load_runtime_dotenv
from config.model_provider import create_chat_model
from eval.dataset_resolver import ResolvedEvaluationDataset, resolve_evaluation_cases
from eval.evaluation_metrics import (
    compute_metrics as compute_result_metrics,
    first_matching_source_rank,
    normalize_source_identifier,
)
from eval.report_generator import count_by_category, write_reports
from eval.verified_snapshot import write_verified_snapshot
from services.service_catalog import normalize_service


REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate the salon AI agent.")
    parser.add_argument("--dataset", default=str(PROJECT_ROOT / "eval" / "golden_dataset.jsonl"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mcp-unavailable-base-url", default="")
    parser.add_argument("--llm-unconfigured-base-url", default="")
    parser.add_argument("--output", default=str(REPORTS_DIR / "latest_summary.json"))
    parser.add_argument("--markdown", default=str(REPORTS_DIR / "latest_summary.md"))
    parser.add_argument("--results-json", default=str(REPORTS_DIR / "latest_results.json"))
    parser.add_argument("--results-csv", default=str(REPORTS_DIR / "latest_results.csv"))
    parser.add_argument(
        "--evaluation-base-date",
        default="",
        help="Future weekday used for EVAL_DATE placeholders (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--verified-snapshot-dir",
        default="",
        help="Write a redacted verified snapshot after a complete real run.",
    )
    parser.add_argument(
        "--corpus-version",
        default=os.getenv("SALON_KNOWLEDGE_CORPUS_VERSION", "salon_knowledge@2026.07"),
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args()


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        case["_line_no"] = line_no
        cases.append(case)
    return cases


class HttpClient:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    def request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_trace_id = trace_id or f"eval-{int(time.time() * 1000)}"
        start = time.perf_counter()
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=json_body,
            params=params,
            timeout=self.timeout,
            headers={"X-Trace-ID": request_trace_id},
        )
        latency_ms = (time.perf_counter() - start) * 1000
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        return {
            "status": response.status_code,
            "body": body,
            "latency_ms": latency_ms,
            "trace_id": response.headers.get("X-Trace-ID", request_trace_id),
        }


async def classify_with_llm(text: str) -> str:
    classifier = TaskClassifier(create_chat_model(temperature=0))
    return await classifier.classify_task(text)


async def call_mcp_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    assert_external_call_allowed(
        "mcp:knowledge-service",
        "eval.run_evaluation.call_mcp_tool",
    )
    load_runtime_dotenv()
    server_python = os.getenv("RAG_MCP_SERVER_PYTHON", "")
    server_cwd = os.getenv("RAG_MCP_SERVER_CWD", "")
    if not server_python or not server_cwd:
        raise RuntimeError("RAG_MCP_SERVER_PYTHON and RAG_MCP_SERVER_CWD are required for direct MCP evaluation")
    params = StdioServerParameters(
        command=server_python,
        args=["-m", os.getenv("RAG_MCP_SERVER_MODULE", "src.mcp_server.server")],
        cwd=server_cwd,
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            tools_result = await session.list_tools()
            result = await session.call_tool("query_knowledge_hub", arguments)
            text = "\n".join(getattr(item, "text", "") for item in result.content if getattr(item, "text", ""))
            return {
                "server": str(getattr(init_result, "serverInfo", None) or getattr(init_result, "server_info", None)),
                "tools": [tool.name for tool in tools_result.tools],
                "is_error": bool(getattr(result, "isError", False) or getattr(result, "is_error", False)),
                "text": text,
                "citations": _extract_citations(text),
            }


def _extract_citations(text: str) -> List[Dict[str, Any]]:
    import re

    match = re.search(r"\*\*References \(JSON\):\*\*\s*```json\s*(.*?)\s*```", text, re.S)
    if not match:
        match = re.search(r"References \(JSON\):\s*```json\s*(.*?)\s*```", text, re.S)
    if not match:
        match = re.search(r"```json\s*(.*?)\s*```", text, re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return payload.get("citations") or []


def evaluate_booking_case(client: HttpClient, case: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    setup_response = None
    if case.get("setup_request_json"):
        setup_response = client.request(
            "POST",
            "/api/appointment/create",
            case["setup_request_json"],
            trace_id=f"eval-{case['id']}-setup",
        )
    response = client.request(
        "POST",
        "/api/appointment/create",
        case.get("request_json", {}),
        trace_id=f"eval-{case['id']}",
    )
    actual_result = _business_result_from_booking_response(response)
    contract_pass = response["status"] == case["expected_http_status"]
    error = ""
    if setup_response and setup_response["status"] not in {200, case["expected_http_status"]}:
        error = f"setup_status={setup_response['status']}"
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=None,
        response=response,
        actual_route="appointment",
        actual_tool_or_service="appointment_service",
        actual_business_result=actual_result,
        error=error,
    )


def evaluate_appointment_modify_case(
    client: HttpClient,
    case: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Create, read, and modify one appointment through public APIs only."""
    setup_request = dict(case.get("setup_request_json") or {})
    actor_id = str(setup_request.get("user_id") or "")
    setup_response = client.request(
        "POST",
        "/api/appointment/create",
        setup_request,
        trace_id=f"eval-{case['id']}-setup",
    )
    setup_body = (
        setup_response.get("body")
        if isinstance(setup_response.get("body"), dict)
        else {}
    )
    setup_data = (
        setup_body.get("data")
        if isinstance(setup_body.get("data"), dict)
        else {}
    )
    appointment_id = setup_data.get("appointment_id")
    stylist_id = setup_data.get("stylist_id")
    original_start = setup_data.get("start_time")

    if setup_response["status"] != 200 or not appointment_id or not actor_id:
        return _case_result(
            case=case,
            context=context,
            contract_pass=False,
            retrieval_quality_pass=None,
            response=setup_response,
            actual_route="appointment",
            actual_tool_or_service="appointment_service",
            actual_business_result={
                "setup_status": setup_response["status"],
                "setup_created": bool(appointment_id),
            },
            error="appointment setup did not return an appointment_id",
        )

    detail_before = client.request(
        "GET",
        f"/api/appointment/{appointment_id}",
        params={"user_id": actor_id},
        trace_id=f"eval-{case['id']}-before",
    )
    before_data = _appointment_from_lifecycle_response(detail_before)
    original_version = before_data.get("version")
    original_date = str(before_data.get("start_time") or original_start)[:10]

    update_request = dict(case.get("update_request_json") or {})
    update_request["user_id"] = actor_id
    update_request["expected_version"] = original_version
    response = client.request(
        "PATCH",
        f"/api/appointment/{appointment_id}",
        update_request,
        trace_id=f"eval-{case['id']}",
    )
    updated = _appointment_from_operation_response(response)
    updated_start = str(updated.get("start_time") or "")
    updated_version = updated.get("version")
    updated_stylist_id = updated.get("stylist_id") or stylist_id
    updated_date = updated_start[:10]

    old_schedule = _read_schedule(
        client,
        int(stylist_id),
        original_date,
        f"eval-{case['id']}-old-schedule",
    )
    new_schedule = _read_schedule(
        client,
        int(updated_stylist_id),
        updated_date,
        f"eval-{case['id']}-new-schedule",
    )
    old_released = not _has_busy_schedule(
        old_schedule,
        appointment_id,
        start_time=original_start,
    )
    new_occupied = _has_busy_schedule(
        new_schedule,
        appointment_id,
        start_time=updated_start,
    )
    expected_start = _expected_modified_start(case)
    status_ok = response["status"] == case["expected_http_status"]
    version_incremented = (
        isinstance(original_version, int)
        and updated_version == original_version + 1
    )
    time_changed = bool(
        expected_start
        and updated_start.startswith(expected_start.replace(" ", "T"))
        and updated_start != str(original_start).replace(" ", "T")
    )
    operation_status = (
        response.get("body", {}).get("data", {}).get("status")
        if isinstance(response.get("body"), dict)
        else None
    )
    contract_pass = all((
        status_ok,
        operation_status == "success",
        time_changed,
        version_incremented,
        old_released,
        new_occupied,
    ))
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=None,
        response=response,
        actual_route="appointment",
        actual_tool_or_service="appointment_service",
        actual_business_result={
            "status": operation_status,
            "appointment_id": appointment_id,
            "original_start_time": original_start,
            "updated_start_time": updated_start,
            "original_version": original_version,
            "updated_version": updated_version,
            "old_schedule_released": old_released,
            "new_schedule_busy": new_occupied,
        },
        error="" if contract_pass else "appointment modification contract failed",
    )


def evaluate_rag_case(client: HttpClient, case: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    response = client.request(
        "POST",
        "/api/consultation/query",
        case.get("request_json", {}),
        trace_id=f"eval-{case['id']}",
    )
    body = response["body"] if isinstance(response["body"], dict) else {}
    status_ok = response["status"] == case["expected_http_status"]
    expected_mode = case["expected_retrieval_mode"]
    actual_mode = body.get("retrieval_mode")
    retrieval_mode_ok = expected_mode == "not_applicable" or actual_mode == expected_mode
    sources = body.get("sources") if isinstance(body.get("sources"), list) else []
    first_rank = first_matching_source_rank(
        sources,
        case.get("expected_sources") or [],
    )
    contract_pass = status_ok and retrieval_mode_ok
    retrieval_quality_pass = _retrieval_quality_pass(case, sources, first_rank)
    actual_result = {
        "status": response["status"],
        "retrieval_mode": actual_mode,
        "source_count": len(sources),
        "matched_rank": first_rank,
        "llm_status": body.get("llm_status"),
    }
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=retrieval_quality_pass,
        response=response,
        actual_route="consultation",
        actual_tool_or_service="mcp_rag",
        actual_business_result=actual_result,
        actual_retrieval_mode=actual_mode,
        returned_sources=_public_sources(sources),
        first_relevant_rank=first_rank,
    )


def evaluate_service_catalog_case(case: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    service = normalize_service(case.get("expected_service") or case.get("input"))
    actual = None
    if service:
        actual = {
            "service": service.name,
            "duration": service.standard_duration,
            "price": service.standard_price,
        }
    contract_pass = bool(service and service.name == case.get("expected_service"))
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=None,
        response={"status": 200, "latency_ms": 0, "trace_id": f"eval-{case['id']}"},
        actual_route="service_catalog",
        actual_tool_or_service="service_catalog",
        actual_business_result=actual,
    )


async def evaluate_task_classifier_case(case: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    start = time.perf_counter()
    actual_route = await classify_with_llm(case["request_json"]["text"])
    latency_ms = (time.perf_counter() - start) * 1000
    contract_pass = actual_route == case["expected_route"]
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=None,
        response={"status": 200, "latency_ms": latency_ms, "trace_id": f"eval-{case['id']}"},
        actual_route=actual_route,
        actual_tool_or_service="task_classifier",
        actual_business_result=actual_route,
    )


async def evaluate_mcp_direct_case(case: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    start = time.perf_counter()
    mcp_result = await call_mcp_tool(case.get("mcp_arguments", {}))
    latency_ms = (time.perf_counter() - start) * 1000
    citations = mcp_result.get("citations") or []
    sources = [{"title": item.get("title", ""), "source": item.get("source", "")} for item in citations]
    first_rank = first_matching_source_rank(
        sources,
        case.get("expected_sources") or [],
    )
    no_result_expected = case.get("expected_business_result") == "no_result_handled"
    contract_pass = (
        not mcp_result.get("is_error")
        and (not no_result_expected or len(sources) == 0)
        and "query_knowledge_hub" in (mcp_result.get("tools") or [])
    )
    retrieval_quality_pass = _retrieval_quality_pass(case, sources, first_rank)
    actual = {
        "is_error": mcp_result.get("is_error"),
        "source_count": len(sources),
        "tools": mcp_result.get("tools"),
    }
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=retrieval_quality_pass,
        response={"status": 200, "latency_ms": latency_ms, "trace_id": f"eval-{case['id']}"},
        actual_route="mcp_direct",
        actual_tool_or_service="query_knowledge_hub",
        actual_business_result=actual,
        actual_retrieval_mode="mcp_hybrid_search",
        returned_sources=_public_sources(sources),
        first_relevant_rank=first_rank,
    )


def evaluate_mcp_unavailable_case(
    client: Optional[HttpClient],
    case: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    if client is None:
        return _skipped_case(case, context, "mcp_unavailable_base_url not provided")
    response = client.request(
        "POST",
        "/api/consultation/query",
        case.get("request_json", {}),
        trace_id=f"eval-{case['id']}",
    )
    body = response.get("body", {}) if isinstance(response.get("body"), dict) else {}
    detail = body.get("detail", {}) if isinstance(body.get("detail"), dict) else {}
    contract_pass = (
        response["status"] == case["expected_http_status"]
        and detail.get("code") == "mcp_rag_unavailable"
        and bool(detail.get("trace_id"))
    )
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=None,
        response=response,
        actual_route="consultation",
        actual_tool_or_service="mcp_rag",
        actual_business_result={"status": response["status"], "detail": detail},
        actual_retrieval_mode="unavailable",
        error="" if contract_pass else str(body),
    )


def evaluate_llm_unconfigured_case(
    client: Optional[HttpClient],
    case: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    if client is None:
        return _skipped_case(case, context, "llm_unconfigured_base_url not provided")
    response = client.request(
        "POST",
        "/api/consultation/query",
        case.get("request_json", {}),
        trace_id=f"eval-{case['id']}",
    )
    body = response["body"] if isinstance(response["body"], dict) else {}
    sources = body.get("sources") if isinstance(body.get("sources"), list) else []
    first_rank = first_matching_source_rank(
        sources,
        case.get("expected_sources") or [],
    )
    contract_pass = (
        response["status"] == case["expected_http_status"]
        and body.get("retrieval_mode") == case["expected_retrieval_mode"]
        and body.get("llm_status") == "not_configured"
    )
    retrieval_quality_pass = _retrieval_quality_pass(case, sources, first_rank)
    return _case_result(
        case=case,
        context=context,
        contract_pass=contract_pass,
        retrieval_quality_pass=retrieval_quality_pass,
        response=response,
        actual_route="consultation",
        actual_tool_or_service="mcp_rag_without_llm",
        actual_business_result={
            "status": response["status"],
            "llm_status": body.get("llm_status"),
            "source_count": len(sources),
        },
        actual_retrieval_mode=body.get("retrieval_mode"),
        returned_sources=_public_sources(sources),
        first_relevant_rank=first_rank,
    )


def _case_result(
    *,
    case: Dict[str, Any],
    context: Dict[str, Any],
    contract_pass: bool,
    retrieval_quality_pass: Optional[bool],
    response: Dict[str, Any],
    actual_route: str,
    actual_tool_or_service: str,
    actual_business_result: Any,
    actual_retrieval_mode: Optional[str] = None,
    returned_sources: Optional[List[Dict[str, Any]]] = None,
    first_relevant_rank: Optional[int] = None,
    error: str = "",
) -> Dict[str, Any]:
    return {
        "id": case["id"],
        "category": case["category"],
        "mode": case.get("evaluation_mode"),
        "input": case.get("input", ""),
        "expected_route": case.get("expected_route"),
        "actual_route": actual_route,
        "expected_tool_or_service": case.get("expected_tool_or_service"),
        "actual_tool_or_service": actual_tool_or_service,
        "expected_http_status": case.get("expected_http_status"),
        "http_status": response.get("status"),
        "expected_retrieval_mode": case.get("expected_retrieval_mode"),
        "actual_retrieval_mode": actual_retrieval_mode,
        "expected_source": case.get("expected_sources") or [],
        "expected_sources": case.get("expected_sources") or [],
        "returned_sources": returned_sources or [],
        "first_relevant_rank": first_relevant_rank,
        "expected_business_result": case.get("expected_business_result"),
        "actual_business_result": actual_business_result,
        "contract_pass": bool(contract_pass),
        "retrieval_quality_pass": retrieval_quality_pass,
        "passed": bool(contract_pass),
        "skipped": False,
        "latency_ms": round(float(response.get("latency_ms", 0)), 2),
        "trace_id": response.get("trace_id", ""),
        "error": error,
        "notes": case.get("notes", ""),
        "required_slots_present": _required_slots_present(case),
        "discovered_tools": (
            list(actual_business_result.get("tools") or [])
            if isinstance(actual_business_result, dict)
            else []
        ),
        "resolved_datetimes": dict(case.get("_resolved_datetimes") or {}),
        "run_timestamp": context["run_timestamp"],
        "corpus_version": context["corpus_version"],
        "model": context["model"],
        "embedding_model": context["embedding_model"],
        "git_commit_sha": context["git_commit_sha"],
    }


def _skipped_case(case: Dict[str, Any], context: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "id": case["id"],
        "category": case["category"],
        "mode": case.get("evaluation_mode"),
        "input": case.get("input", ""),
        "expected_route": case.get("expected_route"),
        "actual_route": None,
        "expected_tool_or_service": case.get("expected_tool_or_service"),
        "actual_tool_or_service": None,
        "expected_http_status": case.get("expected_http_status"),
        "http_status": None,
        "expected_retrieval_mode": case.get("expected_retrieval_mode"),
        "actual_retrieval_mode": None,
        "expected_source": case.get("expected_sources") or [],
        "expected_sources": case.get("expected_sources") or [],
        "returned_sources": [],
        "first_relevant_rank": None,
        "expected_business_result": case.get("expected_business_result"),
        "actual_business_result": None,
        "contract_pass": False,
        "retrieval_quality_pass": None,
        "passed": False,
        "skipped": True,
        "latency_ms": 0,
        "trace_id": "",
        "error": reason,
        "notes": case.get("notes", ""),
        "required_slots_present": _required_slots_present(case),
        "discovered_tools": [],
        "resolved_datetimes": dict(case.get("_resolved_datetimes") or {}),
        "run_timestamp": context["run_timestamp"],
        "corpus_version": context["corpus_version"],
        "model": context["model"],
        "embedding_model": context["embedding_model"],
        "git_commit_sha": context["git_commit_sha"],
    }


def _appointment_from_lifecycle_response(
    response: Dict[str, Any],
) -> Dict[str, Any]:
    body = response.get("body")
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if not isinstance(data, dict):
        return {}
    appointment = data.get("appointment")
    return appointment if isinstance(appointment, dict) else {}


def _appointment_from_operation_response(
    response: Dict[str, Any],
) -> Dict[str, Any]:
    return _appointment_from_lifecycle_response(response)


def _read_schedule(
    client: HttpClient,
    stylist_id: int,
    target_date: str,
    trace_id: str,
) -> List[Dict[str, Any]]:
    if not target_date:
        return []
    response = client.request(
        "GET",
        f"/api/stylists/{stylist_id}/schedule",
        params={"date": target_date},
        trace_id=trace_id,
    )
    return response["body"] if isinstance(response.get("body"), list) else []


def _has_busy_schedule(
    schedules: List[Dict[str, Any]],
    appointment_id: int,
    *,
    start_time: Any,
) -> bool:
    expected_clock = _clock_value(start_time)
    return any(
        item.get("appointment_id") == appointment_id
        and item.get("status") == "busy"
        and _clock_value(item.get("start_time")) == expected_clock
        for item in schedules
    )


def _clock_value(value: Any) -> str:
    text = str(value or "")
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        text = text.split(" ", 1)[1]
    return text[:5]


def _expected_modified_start(case: Dict[str, Any]) -> str:
    update = case.get("update_request_json") or {}
    target_date = str(update.get("target_date") or "")
    target_time = str(update.get("start_time") or "")
    if not target_date or not target_time:
        return ""
    return f"{target_date}T{target_time[:5]}"


def _required_slots_present(case: Dict[str, Any]) -> bool:
    if case.get("evaluation_mode") == "appointment_modify_api":
        setup = case.get("setup_request_json") or {}
        update = case.get("update_request_json") or {}
        return bool(
            setup.get("project")
            and setup.get("start_time")
            and setup.get("duration")
            and (update.get("target_date") or update.get("start_time"))
        )
    request = case.get("request_json") or {}
    return bool(
        request.get("project")
        and request.get("start_time")
        and request.get("duration")
    )


def _business_result_from_booking_response(response: Dict[str, Any]) -> str:
    status = response["status"]
    if status == 200:
        return "booking_confirmed"
    if status == 409:
        return "conflict_blocked"
    if status in {400, 422}:
        return "invalid_booking_rejected"
    return f"http_{status}"


def _retrieval_quality_pass(
    case: Dict[str, Any],
    sources: List[Dict[str, Any]],
    first_rank: Optional[int],
) -> Optional[bool]:
    expected_sources = case.get("expected_sources") or []
    if expected_sources:
        return first_rank is not None
    if case.get("expected_business_result") == "no_result_handled":
        return len(sources) == 0
    return None


def _public_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    public = []
    for source in sources:
        source_name = normalize_source_identifier(
            source.get("source") or source.get("title")
        )
        public.append({
            "title": source.get("title", ""),
            "source": source_name,
            "score": source.get("score", 0.0),
            "page": source.get("page"),
        })
    return public


def compute_metrics(
    cases: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute metrics from case results; cases is retained for API compatibility."""
    del cases
    return compute_result_metrics(results)


def build_run_context(
    args: argparse.Namespace,
    resolved_dataset: ResolvedEvaluationDataset,
    dataset_sha256: str,
) -> Dict[str, Any]:
    return {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_version": args.corpus_version,
        "collection": os.getenv("RAG_MCP_COLLECTION", "salon_knowledge"),
        "model": _model_identifier(),
        "embedding_model": _embedding_model_identifier(),
        "git_commit_sha": _git_sha(PROJECT_ROOT),
        "git_worktree_clean": _git_worktree_clean(PROJECT_ROOT),
        "dataset_sha256": dataset_sha256,
        "evaluation_dates": resolved_dataset.context(),
    }


def _model_identifier() -> str:
    load_runtime_dotenv()
    provider = os.getenv("MODEL_PROVIDER", "openai-compatible")
    model = os.getenv("LLM_MODEL")
    if not model or model.startswith("your_"):
        return "not_configured"
    return f"{provider}:{model}"


def _embedding_model_identifier() -> str:
    load_runtime_dotenv()
    value = os.getenv("EMBEDDING_MODEL") or os.getenv("DASHSCOPE_EMBEDDING_MODEL")
    return value or "unknown"


def _git_sha(cwd: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _git_worktree_clean(cwd: Path) -> bool:
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return not output.strip()
    except Exception:
        return False


async def main_async() -> int:
    args = parse_args()
    load_runtime_dotenv()
    dataset_path = Path(args.dataset)
    raw_cases = load_cases(dataset_path)
    resolved_dataset = resolve_evaluation_cases(
        raw_cases,
        evaluation_base_date=args.evaluation_base_date or None,
    )
    cases = resolved_dataset.cases
    dataset_sha256 = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    context = build_run_context(args, resolved_dataset, dataset_sha256)
    client = HttpClient(args.base_url, args.timeout)
    mcp_unavailable_client = HttpClient(args.mcp_unavailable_base_url, args.timeout) if args.mcp_unavailable_base_url else None
    llm_unconfigured_client = HttpClient(args.llm_unconfigured_base_url, args.timeout) if args.llm_unconfigured_base_url else None

    results: List[Dict[str, Any]] = []
    for case in cases:
        mode = case.get("evaluation_mode")
        if mode == "booking_api":
            results.append(evaluate_booking_case(client, case, context))
        elif mode == "appointment_modify_api":
            results.append(
                evaluate_appointment_modify_case(client, case, context)
            )
        elif mode == "rag_api":
            results.append(evaluate_rag_case(client, case, context))
        elif mode == "service_catalog":
            results.append(evaluate_service_catalog_case(case, context))
        elif mode == "task_classifier":
            results.append(await evaluate_task_classifier_case(case, context))
        elif mode == "mcp_direct":
            results.append(await evaluate_mcp_direct_case(case, context))
        elif mode == "mcp_unavailable_api":
            results.append(evaluate_mcp_unavailable_case(mcp_unavailable_client, case, context))
        elif mode == "llm_unconfigured_api":
            results.append(evaluate_llm_unconfigured_case(llm_unconfigured_client, case, context))
        else:
            results.append(_skipped_case(case, context, f"unsupported evaluation mode: {mode}"))

    report = {
        "generated_at": context["run_timestamp"],
        "base_url": args.base_url,
        "dataset_count": len(cases),
        "samples_by_category": count_by_category(cases),
        "run_context": context,
        "metrics": compute_metrics(cases, results),
        "case_results": results,
    }
    write_reports(
        report,
        Path(args.output),
        Path(args.markdown),
        Path(args.results_json),
        Path(args.results_csv),
    )
    if args.verified_snapshot_dir:
        snapshot_paths = write_verified_snapshot(
            report,
            Path(args.verified_snapshot_dir),
        )
        print(f"Wrote {snapshot_paths[0]}")
        print(f"Wrote {snapshot_paths[1]}")
    failed_contracts = [
        item for item in results
        if not item.get("contract_pass") and not item.get("skipped")
    ]
    skipped = [item for item in results if item.get("skipped")]
    print(f"Evaluated {len(cases)} cases: contract_failed={len(failed_contracts)} skipped={len(skipped)}")
    print(f"Wrote {args.output}")
    print(f"Wrote {args.markdown}")
    print(f"Wrote {args.results_json}")
    print(f"Wrote {args.results_csv}")
    return 1 if failed_contracts else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
