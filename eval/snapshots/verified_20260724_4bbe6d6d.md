# Verified Evaluation Snapshot

- Schema: `1.0`
- Generated at: `2026-07-24T06:07:31.503366+00:00`
- Git commit: `4bbe6d6d67d5c259639d0f216738a33b152a1eab`
- Dataset SHA-256: `1da815ff693ccaa5b0bf82048380f74f3b2044f594f3186b507147fc41d9e5ea`
- Dataset cases: `28`
- Corpus: `salon_knowledge@2026.07`
- Collection: `salon_knowledge`
- Model: `qwen:qwen-plus`
- Embedding model: `text-embedding-v4`

## Aggregate Results

- Functional contract: `28 / 28 = 1.0`
- RAG cases: `11`
- Hit@1: `10 / 11 = 0.9091`
- Hit@3: `11 / 11 = 1.0`
- MRR: `0.9545`

## Case Evidence

| ID | Category | HTTP | Contract | Retrieval | First relevant rank | Latency (ms) |
| --- | --- | ---: | --- | --- | ---: | ---: |
| B001 | booking | 200 | True | None | None | 17.11 |
| B002 | booking | 422 | True | None | None | 0.99 |
| B003 | booking | 422 | True | None | None | 0.85 |
| B004 | booking | 200 | True | None | None | 11.07 |
| B005 | booking | 200 | True | None | None | 40.47 |
| B006 | booking | 400 | True | None | None | 4.89 |
| B007 | booking | 409 | True | None | None | 6.07 |
| B008 | booking | 200 | True | True | 1 | 3851.89 |
| R001 | rag | 200 | True | True | 2 | 4731.36 |
| R002 | rag | 200 | True | True | 1 | 2970.4 |
| R003 | rag | 200 | True | True | 1 | 2047.15 |
| R004 | rag | 200 | True | True | 1 | 3377.76 |
| R005 | rag | 200 | True | True | 1 | 1638.65 |
| R006 | rag | 200 | True | True | 1 | 1430.8 |
| R007 | rag | 200 | True | True | 1 | 5226.19 |
| R008 | rag | 200 | True | True | 1 | 2047.1 |
| T001 | routing | 200 | True | None | None | 727.24 |
| T002 | routing | 200 | True | None | None | 808.54 |
| T003 | routing | 200 | True | None | None | 0.0 |
| T004 | routing | 200 | True | True | 1 | 4755.56 |
| T005 | routing | 200 | True | None | None | 0.0 |
| T006 | routing | 200 | True | None | None | 693.42 |
| E001 | exception | 503 | True | None | None | 26.85 |
| E002 | exception | 200 | True | True | None | 1797.56 |
| E003 | exception | 422 | True | None | None | 2.46 |
| E004 | exception | 200 | True | True | 1 | 965.45 |
| E005 | exception | 409 | True | None | None | 8.82 |
| E006 | exception | 400 | True | None | None | 7.3 |

This file contains redacted aggregate and per-case evidence. It excludes prompts, raw model responses, trace data, identities, credentials, database rows, and local paths.
