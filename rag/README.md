# RAG core

Agentic retrieval over a local folder of PDFs. The model drives a bounded loop with two read-only tools
(`search`, `read_pages`) and answers with the documents' metadata made visible. Backend for the Open WebUI
pipe function in `../functions/payroll_assistant.py`.

## Run

Requires [uv](https://docs.astral.sh/uv/). Python 3.11 is pinned in `.python-version`.

```bash
cd rag
uv sync
export DOCS_DIR=/path/to/pdfs        # default ./docs
uv run rag ingest                    # idempotent: unchanged files are skipped
uv run rag ask "What is the overtime cap?"
uv run rag serve --port 8000         # POST /ask, POST /ingest, GET /health
uv run pytest
```

Point Open WebUI at it with `TRUST_API_URL=http://host.docker.internal:8000/ask` in the repo's `.env`.

## Documents and metadata

Any `*.pdf` under `DOCS_DIR`, recursively. Metadata comes from a sidecar JSON next to the file
(`report.pdf.json`, or `report.json`) with any of `language`, `country`, `department` (HR, Payroll, Time),
`owner`, `created_at`, `updated_at`. Fields not in the sidecar fall through to the LLM extractor hook
(`ingest.null_extractor`, a stub until a provider is chosen) and otherwise stay null. Each field records its
provenance: `sidecar`, `llm` or `missing`. Nothing is ever guessed.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOCS_DIR` | `./docs` | folder of PDFs |
| `INDEX_PATH` | `./data/index.json` | persisted pages, metadata and content hashes |
| `MAX_TOOL_CALLS` | `8` | hard cap per question, then a forced final answer |
| `LLM_PROVIDER` | `fake` | only the scripted fake exists so far |

## Layout

| File | Role |
| --- | --- |
| `ingest.py` | PDF text per page, sidecar metadata, content hashes, JSON index |
| `search.py` | chunking, BM25 (Lucene IDF), the two tools |
| `agent.py` | provider interface, scripted fake, the loop, ref numbering for citations |
| `app.py` | FastAPI `/ask` in the UI's contract, `/ingest`, `/health`, CLI |

Swap points: `search.Index` for another retrieval engine (vectors, hybrid), `agent.LLMProvider` for a real
model, `ingest.Extractor` for LLM metadata extraction.
