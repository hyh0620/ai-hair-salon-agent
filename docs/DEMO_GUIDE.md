# Demo Guide

## Prerequisites

Two independent repositories are expected:

- `ai-hair-salon-agent`
- `mcp-knowledge-service`

Both should use Python 3.11. Do not commit local `.env`, runtime data, logs, or vector indexes.

Weather reminders are optional. Keep `WEATHER_ENABLED=false` unless you are explicitly demonstrating the external Weather Context Tool with a private API key.

## Start MCP Knowledge Service

```bash
cd <PATH_TO_MCP_KNOWLEDGE_SERVICE>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/settings.example.yaml config/settings.yaml
```

Set provider credentials in a private `.env` or shell environment, then ingest:

```bash
python scripts/ingest.py \
  --path examples/salon/generated_pdfs \
  --collection salon_knowledge \
  --force
```

## Start Business App

```bash
cd <PATH_TO_AI_HAIR_SALON_AGENT>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

## Three-Minute Demo

1. Open `/health` and show app, database, MCP RAG, collection, and LLM status.
2. Open `/docs` and show separate appointment and consultation APIs.
3. Create a normal appointment and show price, duration, stylist, and status.
4. Repeat the same stylist/time and show HTTP 409.
5. Ask a consultation question and show citations.
6. Run `eval/mcp_runtime_failure_e2e.py` and show consultation 503 while booking remains available.
7. Optional: enable `WEATHER_ENABLED=true` with a private `OPENWEATHER_API_KEY` and `WEATHER_LOCATION` to show a post-booking travel reminder. This is not part of MCP or RAG.

## Suggested Questions

- `染发前后有什么注意事项？`
- `烫发后多久可以洗头？`
- `临时不能到店，改约规则是什么？`
- `门店几点营业？`
- `男士短发多少钱，需要多久？`

## What To Claim

- The application separates LLM/RAG from deterministic booking rules.
- MCP Knowledge Service provides cited retrieval.
- Appointment success, price, duration, schedule, and conflict checks are deterministic.
- The current evaluation has 28 / 28 functional contracts passing and reports RAG quality separately.
- Optional weather context can enrich a booking success message, but weather failures do not affect booking.

## What Not To Claim

- Do not claim production deployment.
- Do not claim RAG is perfect.
- Do not claim Rerank, Ragas, Memory, Graph RAG, multimodal workflows, Docker, or Kubernetes are implemented in this public release.
- Do not describe the weather context tool as MCP.
