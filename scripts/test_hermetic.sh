#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PYTHON_BIN:-}" ]] && [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi

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

exec "$PYTHON_BIN" -m pytest -W error::DeprecationWarning
