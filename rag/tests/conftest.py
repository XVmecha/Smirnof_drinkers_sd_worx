from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from rag_core.agent import LLMResponse
from rag_core.config import Settings
from rag_core.ingest import ingest
from rag_core.search import Index


def write_pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text, fontsize=11)
    doc.save(path)
    doc.close()


def write_sidecar(pdf: Path, data: dict) -> None:
    pdf.with_name(pdf.name + ".json").write_text(json.dumps(data), encoding="utf-8")


class SequenceProvider:
    """Returns the given responses in order, then a fixed final answer. Records every call."""

    def __init__(self, responses: list[LLMResponse], final: str = "done"):
        self.responses, self.final, self.calls = list(responses), final, []

    def complete(self, system, messages, tools):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self.responses.pop(0) if self.responses else LLMResponse(content=self.final)


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    (docs / "hr").mkdir(parents=True)

    vakantie = docs / "hr" / "vakantiebeleid.pdf"
    write_pdf(vakantie, [
        "Vakantiebeleid Belgie. Elke werknemer heeft recht op 20 wettelijke vakantiedagen per jaar.\nAanvragen gebeuren via het HR portaal.",
        "Extralegale vakantiedagen worden toegekend na een jaar dienst. Overdracht naar het volgende jaar is beperkt.",
    ])
    write_sidecar(vakantie, {"language": "nl", "country": "BE", "department": "hr", "owner": "HR Belgium",
                             "created_at": "2023-01-10", "updated_at": "2025-06-01"})

    paie = docs / "paie_primes.pdf"
    write_pdf(paie, ["Calcul de la prime de fin d'annee. La prime est versee en decembre avec la paie. Le montant depend de l'anciennete."])
    write_sidecar(paie, {"country": "FR", "department": "Finance", "owner": "  "})

    write_pdf(docs / "timesheet_rules.pdf",
              ["Timesheet rules. Employees register worked hours weekly. Overtime requires approval by the team lead."])
    return docs


@pytest.fixture
def settings(docs_dir: Path, tmp_path: Path) -> Settings:
    return Settings(docs_dir=docs_dir, index_path=tmp_path / "data" / "index.json")


@pytest.fixture
def index(settings: Settings) -> Index:
    docs = {}
    ingest(settings, docs)
    return Index(docs)
