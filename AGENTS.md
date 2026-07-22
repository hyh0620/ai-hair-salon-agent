# AGENTS.md

- Main project Python: 3.12. The independent MCP Knowledge Service declares Python 3.11+; keep the two repositories in separate virtual environments.
- Install: `python3.12 -m venv .venv && source .venv/bin/activate && python -m pip install -c constraints-py312.txt -r requirements-dev.txt`.
- Start app: `python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers`.
- Test: `python -m pip check && bash scripts/test_hermetic.sh`. Hermetic tests set `EXTERNAL_CALL_POLICY=deny` and are not real Provider acceptance.
- Full evaluation: run `eval/run_evaluation.py` with normal, MCP-disabled, and LLM-disabled app instances. Real Provider validation is a separate, explicitly allowed flow.
- Configure MCP through `RAG_MCP_SERVER_PYTHON`, `RAG_MCP_SERVER_MODULE`, `RAG_MCP_SERVER_CWD`, and `RAG_MCP_COLLECTION`; FastAPI lifespan starts and manages the stdio child process.
- Booking rules must remain deterministic. RAG must not decide appointment success, price, duration, schedule, or conflicts.
- `.env.example` enables the keyless Open-Meteo weather context by default. It can only run after conversational booking commit and a real `appointment_id`; failure never changes booking facts, REST booking does not append weather, and Hermetic tests block the network call.
- Auth Session, Chat Session, and anonymous owner have different roles. The browser-controlled guest owner is not authentication.
- SQLite transactions protect one local database; they are not distributed transactions. Agent components are role-separated and centrally routed, not a distributed autonomous Agent system.
- Do not commit `.env`, API keys, runtime SQLite databases, ChromaDB data, BM25 indexes, logs, traces, or raw local evaluation dumps.
- Environment setup, Ingest, deep authentication checks and failure injection are documented in `docs/DEMO_RUNBOOK.md`.
