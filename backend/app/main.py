"""Payroll Trust API: POST /ask for the Open WebUI Payroll Assistant.

Request/response contract: see the header of functions/payroll_assistant.py.
"""

import hmac
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .knowledge import KnowledgeBase
from .llm import LLM, Provider

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payroll-trust")

API_TOKEN = os.environ.get("TRUST_API_TOKEN", "")
TOP_K = int(os.environ.get("TOP_K", "6"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))


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
    return [available[name] for name in env_list("LLM_ORDER", "gemini,groq") if name in available]


kb = KnowledgeBase(Path(os.environ.get("DOCS_DIR", "/docs")))
llm = LLM(build_providers())

app = FastAPI(title="Payroll Trust API", docs_url=None, redoc_url=None, openapi_url=None)


class Client(BaseModel):
    id: str = Field(max_length=64)
    name: str = Field(default="", max_length=200)
    country: str = Field(default="", max_length=8)
    joint_committee: str = Field(default="", max_length=16)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    client: Client
    messages: list[Message] = Field(default_factory=list, max_length=100)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Shared-secret check between Open WebUI and this API (skipped when TRUST_API_TOKEN is unset)."""
    if API_TOKEN and not hmac.compare_digest(authorization or "", f"Bearer {API_TOKEN}"):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm_chain": llm.chain, **kb.stats()}


@app.post("/ask", dependencies=[Depends(require_token)])
async def ask(req: AskRequest) -> dict:
    # Search on the question plus the previous user turn, so follow-ups ("and in NL?") still match.
    user_turns = [m.content for m in req.messages if m.role == "user"]
    query = " ".join([*user_turns[-2:-1], req.question])
    hits = kb.search(query, k=TOP_K)
    if not hits:
        return {"answer": "I couldn't find anything about this in the knowledge base.", "sources": [], "conflicts": []}

    if not llm.providers:
        raise HTTPException(status_code=503, detail="No LLM configured: set GEMINI_API_KEY and/or GROQ_API_KEY")

    history = [m.model_dump() for m in req.messages[:-1]] if req.messages else []
    try:
        result = await llm.answer(req.question, req.client.model_dump(), history, hits)
    except Exception as e:
        log.exception("LLM call failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(e).__name__}") from e

    return {
        "answer": result["answer"] or "The model returned no answer.",
        "sources": [h.source() for h in hits],
        "conflicts": result["conflicts"],
        "model": result["model"],
    }
