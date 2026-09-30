"""Idempotent ingestion of a local folder of PDFs into one JSON index file.

Metadata per field: sidecar JSON next to the PDF first, then the LLM extractor hook, else None.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Callable

import pymupdf

from .config import Settings
from .models import FIELDS, Document, IngestReport, Metadata

log = logging.getLogger(__name__)

DEPARTMENTS = {"hr": "HR", "payroll": "Payroll", "time": "Time"}
Extractor = Callable[[str, list[str]], Metadata]


def null_extractor(filename: str, pages: list[str]) -> Metadata:
    """Stands in for LLM extraction until a provider is chosen. Determines nothing."""
    return Metadata()


def sidecar_path(pdf: Path) -> Path | None:
    """`report.pdf` pairs with `report.pdf.json`, falling back to `report.json`."""
    for candidate in (pdf.with_name(pdf.name + ".json"), pdf.with_suffix(".json")):
        if candidate.is_file():
            return candidate
    return None


def load_sidecar(pdf: Path) -> dict:
    path = sidecar_path(pdf)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable sidecar %s: %s", path, exc)
        return {}


def clean(field: str, value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if field == "department":
        return DEPARTMENTS.get(value.lower())  # anything else is dropped, never guessed
    return value


def resolve_metadata(sidecar: dict, extractor: Extractor, filename: str, pages: list[str]) -> tuple[Metadata, dict[str, str]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for field in FIELDS:
        if (value := clean(field, sidecar.get(field))) is not None:
            values[field], sources[field] = value, "sidecar"
    if len(values) < len(FIELDS):
        extracted = extractor(filename, pages).model_dump()
        for field in FIELDS:
            if field not in values and (value := clean(field, extracted.get(field))) is not None:
                values[field], sources[field] = value, "llm"
    return Metadata(**values), {field: sources.get(field, "missing") for field in FIELDS}


def content_hash(pdf: Path) -> str:
    """PDF bytes plus sidecar bytes, so a metadata edit also re-ingests."""
    digest = hashlib.sha256(pdf.read_bytes())
    if (sidecar := sidecar_path(pdf)) is not None:
        digest.update(sidecar.read_bytes())
    return digest.hexdigest()


def extract_pages(pdf: Path) -> list[str]:
    with pymupdf.open(pdf) as doc:
        return [page.get_text("text") for page in doc]


def load_index(path: Path) -> dict[str, Document]:
    if not path.is_file():
        return {}
    return {d["doc_id"]: Document(**d) for d in json.loads(path.read_text(encoding="utf-8"))}


def save_index(path: Path, docs: dict[str, Document]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([d.model_dump() for d in docs.values()], ensure_ascii=False), encoding="utf-8")


def ingest(settings: Settings, docs: dict[str, Document], extractor: Extractor = null_extractor) -> IngestReport:
    """Update `docs` in place from the documents folder and save it. Unchanged files are skipped."""
    report = IngestReport()
    pdfs = sorted(p for p in settings.docs_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf") if settings.docs_dir.is_dir() else []
    seen: set[str] = set()

    for pdf in pdfs:
        rel = pdf.relative_to(settings.docs_dir).as_posix()
        seen.add(rel)
        doc_id = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
        try:
            digest = content_hash(pdf)
            if doc_id in docs and docs[doc_id].content_hash == digest:
                report.skipped.append(rel)
                continue
            pages = extract_pages(pdf)
            metadata, provenance = resolve_metadata(load_sidecar(pdf), extractor, pdf.name, pages)
            docs[doc_id] = Document(doc_id=doc_id, filename=pdf.name, relative_path=rel, content_hash=digest,
                                    metadata=metadata, provenance=provenance, pages=pages)
            report.ingested.append(rel)
            log.info("ingested %s (%d pages)", rel, len(pages))
        except Exception as exc:
            log.exception("failed to ingest %s", rel)
            report.errors[rel] = f"{type(exc).__name__}: {exc}"

    for doc_id, doc in list(docs.items()):
        if doc.relative_path not in seen:
            del docs[doc_id]
            report.removed.append(doc.relative_path)

    save_index(settings.index_path, docs)
    return report
