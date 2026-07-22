# AGENTS.md

- Python version: 3.12.
- Install: `python3.12 -m venv .venv && source .venv/bin/activate && python -m pip install -c constraints-py312.txt -r requirements-dev.txt`.
- Start app: `python -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers`.
- Test: `python -m pip check && bash scripts/test_hermetic.sh`.
- Full evaluation: run `eval/run_evaluation.py` with normal, MCP-disabled, and LLM-disabled app instances.
- MCP service config is required through `RAG_MCP_SERVER_PYTHON`, `RAG_MCP_SERVER_MODULE`, `RAG_MCP_SERVER_CWD`, and `RAG_MCP_COLLECTION`.
- Booking rules must remain deterministic. RAG must not decide appointment success, price, duration, schedule, or conflicts.
- Weather is an Optional Weather Context Tool, not MCP. It is disabled by default and can only append a post-booking reminder after booking succeeds.
- Do not commit `.env`, API keys, runtime SQLite databases, ChromaDB data, BM25 indexes, logs, traces, or raw local evaluation dumps.
- After changing RAG integration, run pytest, `/health`, normal consultation with citations, runtime MCP failure check, normal booking, conflict booking, and full evaluation.
- After changing weather context behavior, run `pytest tests/test_weather_context_tool.py` and confirm structured booking conflict still does not call weather.
