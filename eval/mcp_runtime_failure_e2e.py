"""Verify runtime MCP disconnection behavior against a running FastAPI app.

This is a real E2E script, not a pytest unit test. Start the main app with
RAG_MCP_ENABLED=true before running it.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.external_calls import assert_external_call_allowed


REPORT_PATH = PROJECT_ROOT / "eval" / "reports" / "mcp_runtime_failure_latest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MCP runtime failure E2E check.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--mcp-command-fragment",
        default=os.getenv("RAG_MCP_SERVER_MODULE", "src.mcp_server.server"),
        help="Process command fragment used to identify the child MCP server.",
    )
    return parser.parse_args()


class Client:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    def post(self, path: str, payload: Dict[str, Any], trace_id: str) -> Dict[str, Any]:
        start = time.perf_counter()
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            headers={"X-Trace-ID": trace_id},
            timeout=self.timeout,
        )
        return _response_record(response, start)

    def get(self, path: str, trace_id: str) -> Dict[str, Any]:
        start = time.perf_counter()
        response = self.session.get(
            f"{self.base_url}{path}",
            headers={"X-Trace-ID": trace_id},
            timeout=self.timeout,
        )
        return _response_record(response, start)


def _response_record(response: requests.Response, start: float) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    return {
        "status": response.status_code,
        "body": body,
        "trace_id": response.headers.get("X-Trace-ID", ""),
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }


def find_mcp_server_pids(command_fragment: str) -> List[int]:
    output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    pids: List[int] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if command_fragment in command and "mcp_runtime_failure_e2e.py" not in command:
            pids.append(pid)
    return pids


def terminate_processes(pids: List[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_pid_exists(pid) for pid in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        if _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def booking_payloads() -> tuple[Dict[str, Any], Dict[str, Any]]:
    now = datetime.now() + timedelta(days=35 + (int(time.time()) % 20))
    start = now.replace(hour=10 + (int(time.time()) % 8), minute=0, second=0, microsecond=0)
    start_text = start.strftime("%Y-%m-%d %H:%M")
    suffix = str(int(time.time()))
    first = {
        "user_id": f"mcp_failure_booking_{suffix}",
        "project": "男士短发",
        "start_time": start_text,
        "duration": "45分钟",
        "stylist_id": 1,
        "style_preference": "渐变推剪",
    }
    second = dict(first)
    second["user_id"] = f"mcp_failure_conflict_{suffix}"
    return first, second


def main() -> int:
    assert_external_call_allowed(
        "mcp:knowledge-service",
        "eval.mcp_runtime_failure_e2e.main",
    )
    args = parse_args()
    client = Client(args.base_url, args.timeout)

    report: Dict[str, Any] = {
        "run_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": args.base_url,
        "checks": {},
        "killed_pids": [],
        "errors": [],
    }

    health = client.get("/health", "e2e-mcp-runtime-health")
    report["checks"]["health_before"] = health

    normal = client.post(
        "/api/consultation/query",
        {"question": "染发前后有什么注意事项？"},
        "e2e-mcp-runtime-normal",
    )
    report["checks"]["consultation_before_kill"] = normal
    normal_ok = normal["status"] == 200 and len(normal.get("body", {}).get("sources", []) or []) > 0
    if not normal_ok:
        report["errors"].append("consultation did not return sources before MCP kill")

    pids = find_mcp_server_pids(args.mcp_command_fragment)
    report["killed_pids"] = pids
    if not pids:
        report["errors"].append(f"no MCP server process found with fragment {args.mcp_command_fragment!r}")
    else:
        terminate_processes(pids)
        time.sleep(1.0)

    unavailable = client.post(
        "/api/consultation/query",
        {"question": "烫发后多久可以洗头？"},
        "e2e-mcp-runtime-after-kill",
    )
    report["checks"]["consultation_after_kill"] = unavailable
    detail = unavailable.get("body", {}).get("detail", {}) if isinstance(unavailable.get("body"), dict) else {}
    unavailable_ok = (
        unavailable["status"] == 503
        and isinstance(detail, dict)
        and detail.get("code") == "mcp_rag_unavailable"
        and bool(detail.get("trace_id"))
    )
    if not unavailable_ok:
        report["errors"].append("consultation did not return the mcp_rag_unavailable 503 contract after MCP kill")

    first_payload, second_payload = booking_payloads()
    booking = client.post("/api/appointment/create", first_payload, "e2e-mcp-runtime-booking")
    conflict = client.post("/api/appointment/create", second_payload, "e2e-mcp-runtime-conflict")
    report["checks"]["booking_after_mcp_kill"] = booking
    report["checks"]["conflict_after_mcp_kill"] = conflict
    if booking["status"] != 200:
        report["errors"].append("booking failed after MCP kill")
    if conflict["status"] != 409:
        report["errors"].append("conflict check did not return 409 after MCP kill")

    report["passed"] = not report["errors"]
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {report_path}")
    if report["passed"]:
        print("MCP runtime failure E2E passed")
        return 0
    print("MCP runtime failure E2E failed:")
    for error in report["errors"]:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
