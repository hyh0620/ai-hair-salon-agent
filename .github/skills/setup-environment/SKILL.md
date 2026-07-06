---
name: setup-environment
description: Prepare a local AI Hair Salon Agent checkout for development and verification. Use when setting up dependencies or checking MCP configuration.
---

# Setup Environment

## Inputs

- Project root: current repository.
- Python: `python3.11`.
- Optional MCP Knowledge Service path supplied through `.env`.

## Pipeline

1. Check Python:
   ```bash
   python3.11 --version
   ```
2. Create or reuse virtual environment:
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Create local config if missing:
   ```bash
   test -f .env || cp .env.example .env
   ```
5. Verify required MCP variables are present in `.env` before enabling RAG:
   - `RAG_MCP_SERVER_PYTHON`
   - `RAG_MCP_SERVER_MODULE`
   - `RAG_MCP_SERVER_CWD`
   - `RAG_MCP_COLLECTION`
6. Run dependency check:
   ```bash
   python3.11 -m pip check
   ```

## Output

- Local `.venv`.
- Local `.env` with placeholders.
- Next command: start MCP Knowledge Service, then start FastAPI.

## Failure Handling

- Do not print API keys.
- If MCP path variables are missing, leave `RAG_MCP_ENABLED=false` until configured.
