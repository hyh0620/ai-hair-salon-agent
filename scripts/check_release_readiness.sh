#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "$PROJECT_ROOT"

printf '%s\n' 'Checking Python 3.12'
if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)
PY
then
  printf '%s\n' 'FAIL [python_version] expected Python 3.12' >&2
  exit 1
fi

printf '%s\n' 'Checking dependency consistency'
"$PYTHON_BIN" -m pip check

printf '%s\n' 'Running hermetic test suite'
PYTHON_BIN="$PYTHON_BIN" bash scripts/test_hermetic.sh

printf '%s\n' 'Compiling Python modules'
"$PYTHON_BIN" -m compileall agents api services db config eval web tests

printf '%s\n' 'Checking Git whitespace'
git diff --check

printf '%s\n' 'Checking tracked release hygiene'
"$PYTHON_BIN" - <<'PY'
from pathlib import Path, PurePosixPath
import re
import subprocess

tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
findings: set[tuple[str, str]] = set()
mac_home_prefix = ("/" + "Users/").encode()
linux_home_pattern = re.compile(("/" + r"home/[^/\s<>]+/").encode())

for item in tracked:
    path = PurePosixPath(item)
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if item == ".env.example":
        continue
    if name == ".env" or name.startswith(".env."):
        findings.add(("forbidden_environment_file", item))
    if name in {".ds_store", ".coverage", "coverage.xml", "hosts.yml"}:
        findings.add(("forbidden_runtime_file", item))
    if any(part in parts for part in {
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".codex", "htmlcov", "logs", "chroma", "chromadb",
    }):
        findings.add(("forbidden_runtime_path", item))
    if name.endswith((
        ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
        ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm",
        ".log", ".pyc", ".pkl", ".index",
    )):
        findings.add(("forbidden_runtime_file", item))

    file_path = Path(item)
    try:
        content = file_path.read_bytes()
    except OSError:
        continue
    if mac_home_prefix in content or linux_home_pattern.search(content):
        findings.add(("local_absolute_path", item))

if findings:
    for rule, item in sorted(findings):
        print(f"FAIL [{rule}] {item}")
    raise SystemExit(1)
PY

printf '%s\n' 'Checking release documentation baselines'
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
import re
import subprocess

tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
counts = ("181", "262", "292", "338")
patterns = {
    "stale_test_count": re.compile(
        "|".join(re.escape(value + " passed") for value in counts),
        re.IGNORECASE,
    ),
    "stale_logout_description": re.compile(
        "(?:logout\\s+(?:only|just)\\s+(?:clears?|deletes?).*cookie|"
        "退出只[^。\\n]*cookie|浏览器退出只[^。\\n]*cookie)",
        re.IGNORECASE,
    ),
    "stale_refresh_description": re.compile(
        "(?:no\\s+refresh\\s+token|没有\\s*refresh\\s*token|尚无\\s*refresh\\s*token)",
        re.IGNORECASE,
    ),
    "stale_access_duration": re.compile(
        "(?:access[^\\n]{0,24}480\\s*(?:minutes|分钟)|480\\s*分钟)",
        re.IGNORECASE,
    ),
    "stale_revocation_description": re.compile(
        "(?:认证\\s*token[^。\\n]*不可撤销|authentication token[^.\\n]*not revocable)",
        re.IGNORECASE,
    ),
}
findings: set[tuple[str, str]] = set()
for item in tracked:
    try:
        content = Path(item).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    for rule, pattern in patterns.items():
        if pattern.search(content):
            findings.add((rule, item))

if findings:
    for rule, item in sorted(findings):
        print(f"FAIL [{rule}] {item}")
    raise SystemExit(1)
PY

printf '%s\n' 'Release readiness checks passed'
