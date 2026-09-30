"""Document store + BM25 search over DOCS_DIR.

Reads .pdf, .md and .txt files. Trust metadata (country, owner, updated, ...)
comes from DOCS_DIR/metadata.json, keyed by file name. The index is rebuilt
automatically when a file in the folder is added, removed or changed.
"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from rank_bm25 import BM25Okapi

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".md", ".txt"}
METADATA_FILE = "metadata.json"
CHUNK_CHARS = 800
STOPWORDS = set(
    "a an and are as at be by does do for from has have how in is it of on or that the this to "
    "was what when which who will with de het een en van is dat die voor op te met".split()
)
# Fields passed through to the trust card (see functions/payroll_assistant.py).
META_FIELDS = ("title", "doc_type", "country", "joint_committees", "owner", "expert", "updated", "superseded_by", "url")


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t not in STOPWORDS and len(t) > 1]


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def chunk(text: str) -> list[str]:
    """Pack paragraphs into ~CHUNK_CHARS pieces so excerpts stay readable."""
    chunks, current = [], ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not para:
            continue
        if current and len(current) + len(para) > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


@dataclass
class Doc:
    id: str
    filename: str
    meta: dict
    chunks: list[str]


@dataclass
class Hit:
    doc: Doc
    score: float
    excerpt: str

    def source(self) -> dict:
        meta = {k: self.doc.meta[k] for k in META_FIELDS if self.doc.meta.get(k) not in (None, "")}
        return {"id": self.doc.id, "title": self.doc.filename, **meta, "excerpt": self.excerpt}


@dataclass
class KnowledgeBase:
    docs_dir: Path
    _signature: tuple = ()
    _docs: list[Doc] = field(default_factory=list)
    _chunk_owner: list[int] = field(default_factory=list)
    _chunk_text: list[str] = field(default_factory=list)
    _bm25: BM25Okapi | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _files(self) -> list[Path]:
        if not self.docs_dir.is_dir():
            return []
        return sorted(p for p in self.docs_dir.iterdir() if p.suffix.lower() in SUPPORTED or p.name == METADATA_FILE)

    def refresh(self) -> None:
        files = self._files()
        signature = tuple((p.name, p.stat().st_mtime_ns) for p in files)
        with self._lock:
            if signature == self._signature:
                return
            metadata = {}
            meta_path = self.docs_dir / METADATA_FILE
            if meta_path.exists():
                try:
                    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                except ValueError as e:
                    log.warning("ignoring invalid %s: %s", METADATA_FILE, e)

            docs = []
            for path in files:
                if path.name == METADATA_FILE:
                    continue
                try:
                    chunks = chunk(read_text(path))
                except Exception as e:  # one broken file must not take the index down
                    log.warning("skipping %s: %s", path.name, e)
                    continue
                if chunks:
                    meta = metadata.get(path.name, {}) if isinstance(metadata, dict) else {}
                    docs.append(Doc(id=path.stem, filename=path.name, meta=meta, chunks=chunks))

            corpus, owner, texts = [], [], []
            for i, doc in enumerate(docs):
                for c in doc.chunks:
                    corpus.append(tokenize(f"{doc.meta.get('title', '')} {c}"))
                    owner.append(i)
                    texts.append(c)
            self._docs, self._chunk_owner, self._chunk_text = docs, owner, texts
            self._bm25 = BM25Okapi(corpus) if corpus else None
            self._signature = signature
            log.info("indexed %d documents (%d chunks) from %s", len(docs), len(corpus), self.docs_dir)

    def search(self, query: str, k: int) -> list[Hit]:
        self.refresh()
        tokens = tokenize(query)
        if not self._bm25 or not tokens:
            return []
        best: dict[int, tuple[float, str]] = {}
        for idx, score in enumerate(self._bm25.get_scores(tokens)):
            doc_i = self._chunk_owner[idx]
            if score > 0 and score > best.get(doc_i, (0.0, ""))[0]:
                best[doc_i] = (float(score), self._chunk_text[idx])
        ranked = sorted(best.items(), key=lambda item: item[1][0], reverse=True)[:k]
        return [Hit(doc=self._docs[i], score=s, excerpt=text) for i, (s, text) in ranked]

    def stats(self) -> dict:
        self.refresh()
        return {"documents": len(self._docs), "chunks": len(self._chunk_owner)}
