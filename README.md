# AI Hair Salon Agent

AI Hair Salon Agent is a FastAPI application for hair salon appointment booking and consultation. It combines deterministic booking rules with an MCP-based knowledge retrieval service for non-structured salon knowledge.

The project is designed around a strict boundary:

- Booking success, price, duration, stylist schedule checks, and conflict detection are handled by deterministic backend services and SQLite.
- Salon knowledge such as care guidance, store information, booking policy, and membership rules is retrieved through MCP Knowledge Service.
- RAG never decides whether an appointment succeeds.

## Core Capabilities

- Appointment API with deterministic service catalog, price, duration, business-hour checks, stylist matching, and conflict validation.
- Consultation API backed by an official MCP Python client and `query_knowledge_hub`.
- Hybrid Retrieval through the external MCP Knowledge Service: Dense Retrieval, BM25, RRF Fusion, and citations.
- LangChain 1.x and OpenAI-compatible Qwen chat model support.
- Health checks, request `trace_id`, and explicit MCP failure boundaries.
- Reproducible evaluation with functional-contract metrics and retrieval-quality metrics reported separately.
- Optional Weather Context Tool for post-booking travel reminders. It uses an external weather API only when explicitly configured and never affects booking results.

## Architecture

![Architecture](./architecture.svg)

```text
User request
  -> FastAPI
  -> API route / Agent boundary
  -> Deterministic booking service OR MCP Knowledge Gateway
  -> Optional Weather Context Tool only after conversational booking success
  -> MCP Knowledge Service
  -> Hybrid Retrieval + citations
  -> Response with trace_id
```

The MCP Knowledge Service is a separate repository and process. This business application starts or connects to it through official MCP stdio transport.

## LLM, RAG, And Business Rule Boundary

LLM responsibilities:

- Intent classification.
- Slot extraction in conversational flows.
- Missing-information follow-up.
- Natural-language answer generation from retrieved context.

RAG responsibilities:

- Retrieve salon documents from the configured collection.
- Return citations for consultation answers.
- Explain policies, store information, care guidance, and membership rules.

Deterministic backend responsibilities:

- Normalize service names.
- Compute standard price and duration from `services/service_catalog.py`.
- Validate business hours and stylist schedules.
- Create appointments and block conflicts.
- Update appointment state.

Optional Weather Context Tool:

- Disabled by default through `WEATHER_ENABLED=false`.
- Calls an external weather API only when `WEATHER_ENABLED=true`, `OPENWEATHER_API_KEY`, and `WEATHER_LOCATION` are all configured.
- May append a short travel reminder after a conversational booking has already been saved.
- Does not participate in appointment creation, price, duration, stylist matching, schedule availability, conflict validation, RAG, or MCP.
- If weather is unavailable, the booking response falls back to the normal appointment success message.

## Technology Stack

- Python 3.11
- FastAPI, Uvicorn
- LangChain 1.x
- Qwen or another OpenAI-compatible chat provider
- SQLAlchemy and SQLite
- Official MCP Python SDK
- MCP Knowledge Service with ChromaDB, BM25, RRF, and citations
- pytest

## Project Structure

```text
ai-hair-salon-agent/
├── api/
├── agents/
├── config/
├── db/
├── services/
├── web/
├── tests/
├── eval/
├── docs/
├── .github/skills/
├── AGENTS.md
├── .env.example
├── LICENSE
├── README.md
├── requirements.txt
└── app.py
```

## Environment Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` locally. Do not commit it.

Required MCP settings:

```env
RAG_MCP_ENABLED=true
RAG_MCP_SERVER_PYTHON=<PATH_TO_MCP_KNOWLEDGE_SERVICE>/.venv/bin/python
RAG_MCP_SERVER_MODULE=src.mcp_server.server
RAG_MCP_SERVER_CWD=<PATH_TO_MCP_KNOWLEDGE_SERVICE>
RAG_MCP_COLLECTION=salon_knowledge
RAG_MCP_QUERY_TOP_K=4
```

Optional weather settings:

```env
WEATHER_ENABLED=false
OPENWEATHER_API_KEY=
WEATHER_LOCATION=
WEATHER_TIMEOUT_SECONDS=3
```

Leave weather disabled for normal tests and demos unless you intentionally want to show an external context API. Do not commit a real weather API key.

## Start MCP Knowledge Service

In the MCP Knowledge Service repository:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/settings.example.yaml config/settings.yaml
```

Configure local provider environment variables in a private `.env` file or shell environment. Then ingest the salon example:

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

The business app will start the MCP server with:

```bash
python -m src.mcp_server.server
```

## Start FastAPI

```bash
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Useful endpoints:

- Home: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`
- Stylists: `http://127.0.0.1:8000/stylists`
- Stylist schedule: `http://127.0.0.1:8000/stylist-schedule`
- Knowledge status: `http://127.0.0.1:8000/knowledge`

## Run Tests

```bash
python3.11 -m pip check
python3.11 -m pytest
```

Default pytest uses mocks and deterministic local services. It does not call a real Qwen API or require real API keys.
Weather tests also use mocks and do not call the real OpenWeather service.

## Run Evaluation

Start three app instances for the full evaluation:

```bash
DATABASE_URL=sqlite:////tmp/salon_eval_8000.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000

RAG_MCP_ENABLED=false DATABASE_URL=sqlite:////tmp/salon_eval_8002.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8002

env -u LLM_API_KEY -u LLM_BASE_URL -u LLM_MODEL \
DATABASE_URL=sqlite:////tmp/salon_eval_8003.db \
  python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8003
```

Then run:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/run_evaluation.py \
  --base-url http://127.0.0.1:8000 \
  --mcp-unavailable-base-url http://127.0.0.1:8002 \
  --llm-unconfigured-base-url http://127.0.0.1:8003 \
  --timeout 120
```

The public repository does not include raw local evaluation reports. Current verified summary:

- Functional Contract: 28 / 28
- RAG cases evaluated: 11
- Hit@1: 10 / 11
- Hit@3: 11 / 11
- MRR: 0.9545
- Citation expected-source match: 11 / 11
- MCP runtime failure: consultation returns 503, booking remains available

## MCP Failure Boundary Demo

Start one normal app with MCP enabled, then run:

```bash
NO_PROXY=127.0.0.1,localhost python3.11 eval/mcp_runtime_failure_e2e.py \
  --base-url http://127.0.0.1:8000 \
  --timeout 60
```

Expected behavior:

- Consultation works before the MCP child process is terminated.
- The script terminates the real MCP child process.
- Consultation returns HTTP 503 with `code=mcp_rag_unavailable`.
- Booking creation still returns 200.
- Duplicate stylist/time booking still returns 409.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Evaluation](docs/EVALUATION.md)
- [Demo Guide](docs/DEMO_GUIDE.md)
- [RAG Service Integration](docs/RAG_SERVICE_INTEGRATION.md)

## Skills

Operational skills are stored in `.github/skills/`:

- `setup-environment`
- `run-demo`
- `evaluate-system`
- `update-salon-knowledge`
- `verify-project`

They describe repeatable project workflows and do not contain credentials.

## Known Limits

- The current public evaluation uses a small salon knowledge corpus: 7 documents and 24 chunks.
- Hit@1 is not perfect; retrieval quality is reported separately from functional API contracts.
- Rerank, Ragas, Memory, Graph RAG, multimodal workflows, Docker, and Kubernetes are not part of the verified public release.
- The optional weather tool is not part of the MCP architecture and is not used as evidence for MCP capability.

## Security

Do not commit `.env`, API keys, local runtime databases, ChromaDB files, BM25 indexes, logs, trace files, or raw local evaluation dumps.
