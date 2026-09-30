"""BM25 search over page chunks, plus the two read-only tools the agent can call.

`Index` is the seam for a different engine later (vectors, hybrid): the tools and the agent only use
`search`, `read_pages` and `run_tool`.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from rank_bm25 import BM25Okapi

from .models import Document

TOP_CHUNKS = 20
DOC_LIMIT = 10
PASSAGES_PER_DOC = 3
READ_PAGES_MAX = 5
CHUNK_CHARS = 3000  # about 750 tokens; pieces end up between half and all of this
CHUNK_OVERLAP = 300

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            f"Full-text search over all internal documents. Returns up to {DOC_LIMIT} documents, each with "
            "doc_id, filename, metadata, provenance (where each metadata field came from) and passages: the "
            "matching body text with page numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search terms or a question."}},
            "required": ["query"],
        },
    },
    {
        "name": "read_pages",
        "description": f"Full text of up to {READ_PAGES_MAX} pages of one document, to read around a passage or verify it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "doc_id from a search result."},
                "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "Page numbers, starting at 1."},
            },
            "required": ["doc_id", "pages"],
        },
    },
]

_TOKEN = re.compile(r"[^\W_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip diacritics, split on non-word characters. No stemming, so it is language neutral."""
    normalized = unicodedata.normalize("NFKD", text.lower())
    return _TOKEN.findall("".join(ch for ch in normalized if not unicodedata.combining(ch)))


def chunk(text: str) -> list[str]:
    """Pieces of at most CHUNK_CHARS, cut at whitespace, overlapping by CHUNK_OVERLAP."""
    text = text.strip()
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            cut = max(text.rfind(" ", start + CHUNK_CHARS // 2, end), text.rfind("\n", start + CHUNK_CHARS // 2, end))
            if cut != -1:
                end = cut
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [p for p in pieces if p]


class _LuceneBM25(BM25Okapi):
    """BM25 with the Lucene IDF, log(1 + (N - n + 0.5) / (n + 0.5)), which is always positive.

    The textbook IDF is zero for a term in half the chunks and negative beyond, which on a small corpus
    silently drops the only matching term. Here a score above zero means a query term occurs in the chunk.
    """

    def _calc_idf(self, nd):
        for word, freq in nd.items():
            self.idf[word] = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))


class Index:
    def __init__(self, docs: dict[str, Document]):
        self.docs = docs
        self.chunks: list[tuple[str, int, str]] = [
            (doc.doc_id, page_number, piece)
            for doc in docs.values()
            for page_number, page in enumerate(doc.pages, start=1)
            for piece in chunk(page)
        ]
        self._bm25 = _LuceneBM25([tokenize(text) for _, _, text in self.chunks]) if self.chunks else None

    def search(self, query: str) -> dict[str, Any]:
        passages_by_doc: dict[str, list[dict]] = {}
        for doc_id, page, text in self._top_chunks(query):
            passages = passages_by_doc.setdefault(doc_id, [])
            if len(passages) < PASSAGES_PER_DOC:
                passages.append({"page": page, "text": text})
        documents = [
            {"doc_id": doc.doc_id, "filename": doc.filename, "metadata": doc.metadata.model_dump(),
             "provenance": doc.provenance, "passages": passages}
            for doc_id, passages in list(passages_by_doc.items())[:DOC_LIMIT]
            for doc in [self.docs[doc_id]]
        ]
        return {"query": query, "documents": documents}

    def _top_chunks(self, query: str) -> list[tuple[str, int, str]]:
        tokens = tokenize(query)
        if self._bm25 is None or not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])
        return [self.chunks[i] for i in order[:TOP_CHUNKS]]

    def read_pages(self, doc_id: str, pages: list[int]) -> dict[str, Any]:
        doc = self.docs.get(doc_id)
        if doc is None:
            return {"error": f"Unknown doc_id {doc_id!r}. Use a doc_id from a search result."}
        wanted = sorted({int(p) for p in pages})[:READ_PAGES_MAX]
        return {
            "doc_id": doc.doc_id,
            "filename": doc.filename,
            "page_count": len(doc.pages),
            "pages": [{"page": p, "text": doc.pages[p - 1]} for p in wanted if 1 <= p <= len(doc.pages)],
        }

    def run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "search":
                return self.search(str(arguments.get("query", "")))
            if name == "read_pages":
                return self.read_pages(str(arguments.get("doc_id", "")), list(arguments.get("pages", [])))
            return {"error": f"Unknown tool {name!r}"}
        except Exception as exc:  # the agent gets the error as a tool result instead of a crash
            return {"error": f"{type(exc).__name__}: {exc}"}
