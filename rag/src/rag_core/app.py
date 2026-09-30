"""HTTP API and CLI.

The /ask contract follows functions/payroll_assistant.py on the Open WebUI branch: the UI posts
question, client and messages, and renders answer plus sources.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .agent import LLMProvider, run_agent
from .config import Settings
from .ingest import ingest, load_index
from .models import IngestReport
from .providers import extractor_for, provider_from_env
from .search import Index

log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str
    client: Optional[dict[str, Any]] = None  # sent by the UI, not used yet
    messages: Optional[list[dict[str, Any]]] = None  # sent by the UI, single question only for now


class Source(BaseModel):
    id: str  # the ref number the answer cites as [id]
    title: str
    country: Optional[str] = None
    owner: Optional[str] = None
    updated: Optional[str] = None
    excerpt: str = ""
    # Not in the UI contract yet, but they are the metadata the system is about.
    language: Optional[str] = None
    department: Optional[str] = None
    provenance: dict[str, str] = Field(default_factory=dict)


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


def to_source(doc: dict[str, Any]) -> Source:
    meta = doc["metadata"]
    return Source(id=str(doc["ref"]), title=doc["filename"], country=meta["country"], owner=meta["owner"],
                  updated=meta["updated_at"], excerpt=doc["passages"][0]["text"][:300] if doc["passages"] else "",
                  language=meta["language"], department=meta["department"], provenance=doc["provenance"])


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    provider = provider or provider_from_env()
    extractor = extractor_for(provider)
    docs = load_index(settings.index_path)
    state = {"index": Index(docs)}
    app = FastAPI(title="Trust-aware RAG core")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "documents": len(docs), "chunks": len(state["index"].chunks)}

    @app.post("/ingest", response_model=IngestReport)
    def do_ingest() -> IngestReport:
        report = ingest(settings, docs, extractor)
        state["index"] = Index(docs)
        return report

    @app.post("/ask", response_model=AskResponse)
    def ask(request: AskRequest) -> AskResponse:
        result = run_agent(request.question, state["index"], provider, settings.max_tool_calls)
        log.info("ask: %d tool calls, %d sources, hit_cap=%s", result.tool_calls_used, len(result.sources), result.hit_cap)
        return AskResponse(answer=result.answer, sources=[to_source(d) for d in result.sources])

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag", description="Trust-aware RAG core")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="ingest PDFs from DOCS_DIR (unchanged files are skipped)")
    ask = sub.add_parser("ask", help="run the agent on one question")
    ask.add_argument("question")
    ask.add_argument("--trace", action="store_true", help="also print the tool trace as JSON on stderr")
    serve = sub.add_parser("serve", help="start the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    provider = provider_from_env()

    if args.command == "ingest":
        report = ingest(settings, load_index(settings.index_path), extractor_for(provider))
        print(report.model_dump_json(indent=2))
        return 1 if report.errors else 0
    if args.command == "ask":
        result = run_agent(args.question, Index(load_index(settings.index_path)), provider, settings.max_tool_calls)
        print(result.answer)
        if args.trace:
            print(json.dumps(result.trace, indent=2, ensure_ascii=False), file=sys.stderr)
        return 0
    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(settings, provider), host=args.host, port=args.port)
    return 0
