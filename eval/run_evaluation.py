"""Run reproducible evaluation against the salon AI agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.task_classification.task_classifier import TaskClassifier
from config.model_provider import create_chat_model
from eval.report_generator import count_by_category, write_reports
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
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_trace_id = trace_id or f"eval-{int(time.time() * 1000)}"
        start = time.perf_counter()
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=json_body,
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

    load_dotenv()
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
    first_rank = _first_matching_rank(sources, case.get("expected_sources") or [])
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
    first_rank = _first_matching_rank(sources, case.get("expected_sources") or [])
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
    first_rank = _first_matching_rank(sources, case.get("expected_sources") or [])
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
        "run_timestamp": context["run_timestamp"],
        "corpus_version": context["corpus_version"],
        "model": context["model"],
        "embedding_model": context["embedding_model"],
        "git_commit_sha": context["git_commit_sha"],
    }


def _business_result_from_booking_response(response: Dict[str, Any]) -> str:
    status = response["status"]
    if status == 200:
        return "booking_confirmed"
    if status == 409:
        return "conflict_blocked"
    if status in {400, 422}:
        return "invalid_booking_rejected"
    return f"http_{status}"


def _first_matching_rank(sources: List[Dict[str, Any]], expected_sources: List[str]) -> Optional[int]:
    if not expected_sources:
        return None
    for idx, source in enumerate(sources, 1):
        source_text = str(source.get("source") or source.get("title") or "")
        if any(expected in source_text for expected in expected_sources):
            return idx
    return None


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
        public.append({
            "title": source.get("title", ""),
            "source": source.get("source", ""),
            "score": source.get("score", 0.0),
            "page": source.get("page"),
        })
    return public


def compute_metrics(cases: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_id = {item["id"]: item for item in results}
    evaluated = [item for item in results if not item.get("skipped")]
    booking_cases = [case for case in cases if case.get("evaluation_mode") == "booking_api"]
    route_cases = [case for case in cases if case.get("category") == "routing"]

    expected_success = [case for case in booking_cases if case.get("expected_http_status") == 200]
    expected_conflict = [case for case in booking_cases if case.get("expected_http_status") == 409]
    expected_invalid = [case for case in booking_cases if case.get("expected_http_status") in {400, 422}]
    expected_required_slots = [
        case for case in expected_success
        if case.get("request_json", {}).get("project")
        and case.get("request_json", {}).get("start_time")
        and case.get("request_json", {}).get("duration")
    ]

    route_evaluated = [
        by_id[case["id"]]
        for case in route_cases
        if case["id"] in by_id and not by_id[case["id"]].get("skipped")
    ]
    unnecessary_tool_cases = [
        item for item in route_evaluated
        if item.get("expected_tool_or_service") != "mcp_rag"
    ]
    source_expected = [
        item for item in evaluated
        if item.get("expected_source")
        and item.get("mode") in {"rag_api", "llm_unconfigured_api", "mcp_direct"}
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
        item["latency_ms"]
        for item in evaluated
        if item.get("mode") in {"rag_api", "llm_unconfigured_api", "mcp_unavailable_api"}
    ]
    booking_latencies = [
        item["latency_ms"]
        for item in evaluated
        if item.get("mode") == "booking_api"
    ]

    hit1_n = sum(1 for item in source_expected if item.get("first_relevant_rank") == 1)
    hit3_n = sum(
        1
        for item in source_expected
        if item.get("first_relevant_rank") is not None and item["first_relevant_rank"] <= 3
    )
    source_match_n = sum(1 for item in source_expected if item.get("first_relevant_rank") is not None)
    citation_presence_n = sum(1 for item in source_expected if len(item.get("returned_sources") or []) > 0)
    reciprocal_sum = sum(
        (1 / item["first_relevant_rank"]) if item.get("first_relevant_rank") else 0
        for item in source_expected
    )

    return {
        "functional_contract": {
            "api_contract_pass": _ratio(sum(1 for item in evaluated if item.get("contract_pass")), len(evaluated)),
            "booking_contract_pass": _ratio(
                sum(1 for case in booking_cases if _contract_pass(by_id, case)),
                len(booking_cases),
            ),
            "booking_conflict_contract_pass": _ratio(
                sum(1 for case in expected_conflict if _contract_pass(by_id, case)),
                len(expected_conflict),
            ),
            "mcp_unavailable_contract_pass": _ratio(
                sum(1 for item in mcp_unavailable if item.get("contract_pass")),
                len(mcp_unavailable),
            ),
            "api_error_contract_pass": _ratio(
                sum(1 for item in api_error_contract if item.get("contract_pass")),
                len(api_error_contract),
            ),
        },
        "booking": {
            "booking_success_rate": _ratio(
                sum(1 for case in expected_success if _contract_pass(by_id, case)),
                len(expected_success),
            ),
            "conflict_block_rate": _ratio(
                sum(1 for case in expected_conflict if _contract_pass(by_id, case)),
                len(expected_conflict),
            ),
            "invalid_booking_rejection_rate": _ratio(
                sum(1 for case in expected_invalid if _contract_pass(by_id, case)),
                len(expected_invalid),
            ),
            "required_slot_completion_rate": _ratio(
                sum(1 for case in expected_required_slots if _contract_pass(by_id, case)),
                len(expected_required_slots),
            ),
        },
        "routing": {
            "route_accuracy": _ratio(
                sum(1 for item in route_evaluated if item.get("actual_route") == item.get("expected_route")),
                len(route_evaluated),
            ),
            "tool_or_service_selection_accuracy": _ratio(
                sum(1 for item in route_evaluated if item.get("actual_tool_or_service") == item.get("expected_tool_or_service")),
                len(route_evaluated),
            ),
            "unnecessary_tool_call_rate": _ratio(
                sum(1 for item in unnecessary_tool_cases if item.get("actual_tool_or_service") == "mcp_rag"),
                len(unnecessary_tool_cases),
            ),
        },
        "retrieval_quality": {
            "rag_cases_evaluated": len(source_expected),
            "hit_at_1": _ratio(hit1_n, len(source_expected)),
            "hit_at_3": _ratio(hit3_n, len(source_expected)),
            "mrr": round(reciprocal_sum / len(source_expected), 4) if source_expected else None,
            "mrr_numerator_reciprocal_sum": round(reciprocal_sum, 4),
            "mrr_denominator": len(source_expected),
            "citation_presence_rate": _ratio(citation_presence_n, len(source_expected)),
            "citation_expected_source_match_rate": _ratio(source_match_n, len(source_expected)),
            "empty_result_handling_correctness": _ratio(
                sum(1 for item in no_result_cases if item.get("contract_pass") and not item.get("returned_sources")),
                len(no_result_cases),
            ),
        },
        "stability": {
            "mcp_unavailable_handling_correctness": _ratio(
                sum(1 for item in mcp_unavailable if item.get("contract_pass")),
                len(mcp_unavailable),
            ),
            "api_error_contract_correctness": _ratio(
                sum(1 for item in api_error_contract if item.get("contract_pass")),
                len(api_error_contract),
            ),
            "rag_server_tool_discovery_success": _tool_discovery_success(results),
        },
        "latency": {
            "consultation_api_p50_ms": _percentile(consultation_latencies, 50),
            "consultation_api_p95_ms": _percentile(consultation_latencies, 95),
            "consultation_api_samples": len(consultation_latencies),
            "booking_api_p50_ms": _percentile(booking_latencies, 50),
            "booking_api_p95_ms": _percentile(booking_latencies, 95),
            "booking_api_samples": len(booking_latencies),
        },
    }


def _contract_pass(by_id: Dict[str, Dict[str, Any]], case: Dict[str, Any]) -> bool:
    return bool(by_id.get(case["id"], {}).get("contract_pass"))


def _ratio(numerator: int, denominator: int) -> Dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def _percentile(values: List[float], percentile: int) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    index = round((percentile / 100) * (len(ordered) - 1))
    return round(ordered[index], 2)


def _tool_discovery_success(results: List[Dict[str, Any]]) -> bool:
    for item in results:
        actual = item.get("actual_business_result")
        if isinstance(actual, dict) and "tools" in actual:
            return "query_knowledge_hub" in actual.get("tools", [])
    return False


def build_run_context(args: argparse.Namespace) -> Dict[str, str]:
    return {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_version": args.corpus_version,
        "model": _model_identifier(),
        "embedding_model": _embedding_model_identifier(),
        "git_commit_sha": _git_sha(PROJECT_ROOT),
    }


def _model_identifier() -> str:
    load_dotenv()
    provider = os.getenv("MODEL_PROVIDER", "openai-compatible")
    model = os.getenv("LLM_MODEL")
    if not model or model.startswith("your_"):
        return "not_configured"
    return f"{provider}:{model}"


def _embedding_model_identifier() -> str:
    load_dotenv()
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


async def main_async() -> int:
    args = parse_args()
    load_dotenv()
    cases = load_cases(Path(args.dataset))
    context = build_run_context(args)
    client = HttpClient(args.base_url, args.timeout)
    mcp_unavailable_client = HttpClient(args.mcp_unavailable_base_url, args.timeout) if args.mcp_unavailable_base_url else None
    llm_unconfigured_client = HttpClient(args.llm_unconfigured_base_url, args.timeout) if args.llm_unconfigured_base_url else None

    results: List[Dict[str, Any]] = []
    for case in cases:
        mode = case.get("evaluation_mode")
        if mode == "booking_api":
            results.append(evaluate_booking_case(client, case, context))
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
