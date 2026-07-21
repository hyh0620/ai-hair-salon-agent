#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PYTHON_BIN:-}" ]] && [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi
HOST=127.0.0.1
PORT="${PORT:-8000}"
RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ai-hair-salon-validation.XXXXXX")"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$RUNTIME_DIR"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$PROJECT_ROOT"

export EXTERNAL_CALL_POLICY=deny
export MODEL_PROVIDER=qwen
export LLM_API_KEY=
export LLM_BASE_URL=
export LLM_MODEL=
export LLM_HTTP_LOCAL_ADDRESS=
export EMBEDDING_PROVIDER=qwen
export EMBEDDING_API_KEY=
export EMBEDDING_BASE_URL=
export EMBEDDING_MODEL=
export AZURE_OPENAI_API_KEY=
export AZURE_OPENAI_ENDPOINT=
export AZURE_OPENAI_DEPLOYMENT=
export AZURE_OPENAI_VERSION=
export AZURE_OPENAI_DEPLOYMENT_EMBEDDING=
export AZURE_OPENAI_ENDPOINT_EMBEDDING=
export AZURE_OPENAI_EMBEDDING_VERSION=
export RAG_MCP_ENABLED=false
export RAG_MCP_SERVER_PYTHON=
export RAG_MCP_SERVER_CWD=
export WEATHER_ENABLED=false
export OPENWEATHER_API_KEY=
export AUTH_ENABLED=true
export AUTH_JWT_SECRET="$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL="sqlite:///$RUNTIME_DIR/validation.db"

printf '%s\n' \
  "Isolated validation mode" \
  "External providers: denied" \
  "Database: temporary" \
  "Host: $HOST" \
  "Port: $PORT"

"$PYTHON_BIN" -m uvicorn app:app --host "$HOST" --port "$PORT" --no-proxy-headers &
SERVER_PID=$!
wait "$SERVER_PID"
