from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

FIELDS = ("language", "country", "department", "owner", "created_at", "updated_at")


class Metadata(BaseModel):
    """None means the document does not state the value. Values are never guessed."""

    language: Optional[str] = None
    country: Optional[str] = None
    department: Optional[Literal["HR", "Payroll", "Time"]] = None
    owner: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Document(BaseModel):
    doc_id: str
    filename: str
    relative_path: str
    content_hash: str
    metadata: Metadata
    provenance: dict[str, str]  # per field: "sidecar", "llm" or "missing"
    pages: list[str]  # text per page, index 0 is page 1


class IngestReport(BaseModel):
    ingested: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
