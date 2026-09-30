"""The document catalog as the only metadata source: access scope, relationship flags, and <document> tags.

Two rules keep data out of the prompt by construction:
- `scope` is the only function that sees the whole catalog. Flags, candidates and tags are built from its result,
  so another client's documents cannot reach the model through a relationship link.
- Tag attributes come from the allowlist `TAG_FIELDS`. A catalog field reaches the model only once it is added there.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

TAG_FIELDS = ("id", "title", "layer", "country", "domain", "client_id", "entity",
              "source_type", "version", "status", "owner", "last_updated")
OWNER_FLAGS = {"left": "owner_left", "moved_team": "owner_moved", "none": "no_owner"}
STATUS_FLAGS = ("draft", "informal")
CLIENTS_DIR = "clients/"


class AccessDenied(Exception):
    """The consultant is unknown or not assigned to the selected client. Reveal nothing further."""


def load_catalog(data_dir: Path) -> list[dict[str, Any]]:
    return json.loads((data_dir / "catalog.json").read_text(encoding="utf-8"))["documents"]


def load_consultants(data_dir: Path) -> dict[str, dict[str, Any]]:
    users = json.loads((data_dir / "users.json").read_text(encoding="utf-8"))
    return {c["id"]: c for c in users["consultants"]}


def document_client(doc: dict[str, Any]) -> str | None:
    """The client a document belongs to: its client_id, else the `clients/<id>_<slug>/` folder it sits in."""
    if doc.get("client_id"):
        return doc["client_id"]
    path = doc.get("path", "")
    if path.startswith(CLIENTS_DIR):
        return path[len(CLIENTS_DIR):].split("/", 1)[0].split("_", 1)[0] or None
    return None


def scope(catalog: list[dict[str, Any]], consultant: dict[str, Any] | None, client_id: str) -> list[dict[str, Any]]:
    """Every document usable for this consultant working for this client: general ones plus that client's own.

    Other clients' documents are left out even when the consultant may see them, so one client's rules never
    show up as flags while working for another. A document under clients/ whose client cannot be determined
    is left out (fail closed).
    """
    if consultant is None or client_id not in consultant.get("clients", []):
        raise AccessDenied(client_id)
    kept = []
    for doc in catalog:
        owner = document_client(doc)
        if owner == client_id or (owner is None and not doc.get("path", "").startswith(CLIENTS_DIR)):
            kept.append(doc)
    return kept


def compute_flags(documents: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Flags per document id, from links inside `documents` only. A link to a document outside it is dropped."""
    ids = {d["id"] for d in documents}
    flags: dict[str, list[str]] = {d["id"]: [] for d in documents}
    for doc in documents:
        for field, forward, backward in (("supersedes", "supersedes", "superseded_by"), ("overrides", "overrides", "overridden_by")):
            target = doc.get(field)
            if target in ids:
                flags[doc["id"]].append(f"{forward}:{target}")
                flags[target].append(f"{backward}:{doc['id']}")
    for doc in documents:
        if (flag := OWNER_FLAGS.get(doc.get("owner_status") or "")) is not None:
            flags[doc["id"]].append(flag)
        if doc.get("status") in STATUS_FLAGS:
            flags[doc["id"]].append(doc["status"])
    return flags


def document_tag(doc: dict[str, Any], flags: list[str], text: str) -> str:
    """Escaped, so neither attribute values nor document text can close the tag or open a new one."""
    attrs = {k: str(doc[k]) for k in TAG_FIELDS if doc.get(k) is not None}
    attrs["flags"] = " ".join(flags)
    rendered = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
    return f"<document {rendered}>\n{html.escape(text.strip(), quote=False)}\n</document>"
