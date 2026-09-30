"""Payroll Trust API: POST /ask for the Open WebUI Payroll Assistant.

Flow (README "Access control"): identify the consultant, determine the client,
check access, build the allowed document set, and only then search and ask the
LLM. Code decides which documents the model sees and which facts (flags) are
attached to each; the model only ranks them and says what stands out (relevance.py).
Request/response contract: see the header of functions/payroll_assistant.py.
"""

import hmac
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .catalog import scope
from .knowledge import KnowledgeBase
from .llm import LLM, Provider
from .relevance import prepare

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payroll-trust")

API_TOKEN = os.environ.get("TRUST_API_TOKEN", "")
TOP_K = int(os.environ.get("TOP_K", "8"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))
FALLBACK_EXPERT = "Knowledge Team"

# Country names match in any case; the codes only in capitals, so the word "be" doesn't count.
COUNTRY_WORDS = {
    "BE": (r"\b(belgium|belgian|belgi[eë]|belgique)\b", r"\bBE\b"),
    "NL": (r"\b(netherlands|dutch|nederland|holland)\b", r"\bNL\b"),
}


def env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def build_providers() -> list[Provider]:
    available = {}
    if os.environ.get("GEMINI_API_KEY"):
        available["gemini"] = Provider.create(
            "gemini",
            os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
            os.environ["GEMINI_API_KEY"],
            [os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
             *env_list("GEMINI_FALLBACK_MODELS", "gemini-flash-latest,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest")],
            TIMEOUT,
        )
    if os.environ.get("GROQ_API_KEY"):
        available["groq"] = Provider.create(
            "groq",
            os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            os.environ["GROQ_API_KEY"],
            env_list("GROQ_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b"),
            TIMEOUT,
        )
    return [available[name] for name in env_list("LLM_ORDER", "groq,gemini") if name in available]


kb = KnowledgeBase(Path(os.environ.get("DATA_DIR", "/data")))
llm = LLM(build_providers())

app = FastAPI(title="Payroll Trust API", docs_url=None, redoc_url=None, openapi_url=None)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    # Identity comes from the Open WebUI session (the selected consultant), never from the question text.
    consultant_id: str = Field(max_length=32)
    messages: list[Message] = Field(default_factory=list, max_length=100)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Shared-secret check between Open WebUI and this API (skipped when TRUST_API_TOKEN is unset)."""
    if API_TOKEN and not hmac.compare_digest(authorization or "", f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---- context detection ------------------------------------------------------


def detect_country(text: str) -> str | None:
    found = [c for c, (names, code) in COUNTRY_WORDS.items() if re.search(names, text, re.I) or re.search(code, text)]
    return found[0] if len(found) == 1 else None


def detect_site(client: dict, text: str) -> str | None:
    """The country of the one entity whose site the text names ("the Antwerp site" -> BE)."""
    found = {e["country"] for e in client["entities"] if e.get("site") and re.search(rf"\b{re.escape(e['site'])}\b", text, re.I)}
    return found.pop() if len(found) == 1 else None


def detect_client(text: str) -> str | None:
    lowered = text.lower()
    for cid, client in kb.clients.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in client["aliases"]):
            return cid
    return None


def latest(turns: list[str], detector):
    """First turn (newest first) where the detector finds something."""
    for text in turns:
        if (value := detector(text)) is not None:
            return value
    return None


def reply(status: str, answer: str, context: dict, **extra) -> dict:
    return {"status": status, "answer": answer, "sources": [], "notes": [], "context": context, **extra}


# ---- documents for the card ------------------------------------------------

# Catalog fields shown to the consultant exactly as stored (the prompt promises this). Not shown:
# path (a storage detail), topics (search keywords) and security_test (an evaluation label).
DISPLAY_FIELDS = ("id", "title", "layer", "country", "domain", "client_id", "entity", "source_type", "version",
                  "status", "owner", "owner_status", "last_updated", "supersedes", "overrides")


def to_source(doc_id: str, flags: list[str], group: str, note: str) -> dict:
    doc = kb.docs[doc_id]
    meta = {k: doc.meta[k] for k in DISPLAY_FIELDS if doc.meta.get(k) is not None}
    return {**meta, "group": group, "flags": flags, "note": note, "excerpt": doc.text}


# ---- endpoints --------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    kb.refresh()
    return {"ok": True, "llm_chain": llm.chain, "documents": len(kb.docs), "consultants": len(kb.users)}


@app.post("/ask", dependencies=[Depends(require_token)])
async def ask(req: AskRequest) -> dict:
    kb.refresh()
    consultant = kb.users.get(req.consultant_id)
    if not consultant:
        raise HTTPException(status_code=403, detail="Unknown consultant")

    # Newest first: the question, then earlier user turns (for follow-ups like "same for the Dutch entity").
    earlier = [m.content for m in req.messages if m.role == "user"]
    if earlier and earlier[-1] == req.question:
        earlier = earlier[:-1]
    turns = [req.question, *reversed(earlier)]

    client_id = latest(turns, detect_client)
    client = kb.clients.get(client_id) if client_id else None
    context = {"consultant": consultant["name"], "client_id": client_id, "client": client["name"] if client else None}

    # 1. Access check, in code, before anything is searched.
    if client_id and client_id not in consultant.get("clients", []):
        log.warning("access denied: consultant=%s client=%s", consultant["id"], client_id)
        return reply("denied", "You are not assigned to this client, so I can't search or share its documents. "
                     "Ask the account team if you need access.", {"consultant": consultant["name"]})

    # 2. Country / entity, from a country name or an entity's site. A client with one entity implies its country.
    country = latest(turns, lambda t: detect_country(t) or (detect_site(client, t) if client else None))
    if client and not country and len(client["entities"]) == 1:
        country = client["entities"][0]["country"]
    entity = next((e for e in client["entities"] if e["country"] == country), None) if client else None
    context |= {"country": country, "entity": entity["name"] if entity else None,
                "sector": entity["sector"] if entity else None}

    # 3. Allowed set: general documents and only this (permitted) client's own (catalog.scope, the single access rule).
    catalog = [d.meta for d in kb.docs.values()]
    allowed = {d["id"] for d in scope(catalog, consultant, client_id)}
    query = " ".join([req.question, *(earlier[-1:] if client_id else [])])
    hits = kb.search(query, allowed, TOP_K)

    if not any(h.topic_match for h in hits):
        expert = kb.expert_for(country, req.question)
        return reply("not_found", "I couldn't find anything about this in the knowledge base, so I won't guess. "
                     f"Ask {expert or FALLBACK_EXPERT}.", context, expert=expert or FALLBACK_EXPERT)

    if client and not country:
        options = " or ".join(f"{e['name']} ({e['country']})" for e in client["entities"])
        return reply("clarify", f"Which entity is this for: {options}?", context)
    if not client and not country:
        return reply("clarify", "Which client (and country) is this about? Rates can differ per client agreement.", context)

    # 4. Code decides the documents and their facts; the model ranks and describes them.
    history = [m.model_dump() for m in req.messages if m.content != req.question][-6:]
    prepared = prepare(catalog, consultant, client_id, client["name"] if client else "", req.question,
                       {d.id: d.text for d in kb.docs.values()},
                       context={"country": country, "entity": context["entity"], "domain": None},
                       history=history, candidates=[h.doc.id for h in hits], today=date.today())
    best_topics = " ".join(hits[0].doc.meta.get("topics", []))
    expert = kb.expert_for(country, f"{req.question} {best_topics}")

    if not llm.providers:
        raise HTTPException(status_code=503, detail="No LLM configured: set GROQ_API_KEY and/or GEMINI_API_KEY")
    try:
        result = await llm.describe(prepared.prompt, req.question, prepared.main, prepared.other)
    except Exception as e:
        log.exception("LLM call failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(e).__name__}") from e

    # Sources in citation order: [n] is the position in ranking, then other_country (see the prompt).
    sources = [to_source(e["id"], prepared.flags[e["id"]], "ranking", e["notable"]) for e in result["ranking"]]
    sources += [to_source(e["id"], prepared.flags[e["id"]], "other_country", e["notable"]) for e in result["other_country"]]
    sources += [to_source(e["id"], prepared.flags[e["id"]], "suspicious", e["reason"]) for e in result["suspicious"]]
    return {
        "status": "answered",
        "answer": result["answer"] or "The model returned no summary.",
        "sources": sources,
        "notes": result["notes"],
        "shown": prepared.main + prepared.other,  # every document the model was given, in prompt order
        "context": context,
        "expert": expert or FALLBACK_EXPERT,
        "model": result["model"],
    }
