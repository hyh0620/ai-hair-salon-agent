---
name: update-salon-knowledge
description: Update the salon example knowledge source in MCP Knowledge Service and reingest it into the salon_knowledge collection.
---

# Update Salon Knowledge

## Inputs

- MCP Knowledge Service repository path.
- Updated source documents under `examples/salon/`.
- Target collection: `salon_knowledge`.

## Pipeline

1. Edit the Markdown source files in MCP Knowledge Service.
2. Generate or verify PDFs under `examples/salon/generated_pdfs`.
3. Run ingestion:
   ```bash
   python scripts/ingest.py \
     --path examples/salon/generated_pdfs \
     --collection salon_knowledge \
     --force
   ```
4. Verify query:
   ```bash
   python scripts/query.py \
     --query "染发前后有什么注意事项？" \
     --collection salon_knowledge \
     --top-k 4
   ```
5. Re-run this app's consultation evaluation.

## Output

- File count.
- Chunk count.
- Vector count.
- BM25 document count.
- Retrieval regression status.

## Rules

- Do not commit ChromaDB data, BM25 index files, logs, traces, or raw reports.
