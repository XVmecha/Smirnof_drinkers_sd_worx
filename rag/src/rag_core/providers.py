"""Real model adapters behind `agent.LLMProvider`, provider selection, and the LLM metadata extractor."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx

from .agent import LLMProvider, LLMResponse, Message, ScriptedFakeProvider, ToolCall
from .ingest import Extractor, clean, null_extractor
from .models import FIELDS, Metadata

log = logging.getLogger(__name__)


class OpenAICompatibleProvider:
    """Chat completions with tools. Works for OpenAI, Ollama (`<host>:11434/v1`) and other compatible servers."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120, transport: httpx.BaseTransport | None = None):
        self.model = model
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                    headers={"Authorization": f"Bearer {api_key or 'none'}"})

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]] | None) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": [{"role": "system", "content": system}, *map(_to_openai, messages)]}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
        for attempt in range(5):  # rate limits and overload: retry, honouring the server's retry hint when it gives one
            response = self._client.post("/chat/completions", json=body)
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 4:
                break
            wait = min(_retry_after(response) or 2 ** attempt, 60)
            log.warning("%s from %s, retrying in %.0fs", response.status_code, self._client.base_url, wait)
            time.sleep(wait)
        if response.status_code >= 400:  # keep the body: it says what the server rejected
            raise RuntimeError(f"{response.status_code} from {response.url}: {response.text[:800]}")
        message = response.json()["choices"][0]["message"]
        calls = [ToolCall(id=c.get("id") or f"call_{i}", name=c["function"]["name"], arguments=_parse_arguments(c["function"].get("arguments")),
                          extra={k: v for k, v in c.items() if k not in ("id", "type", "function")})
                 for i, c in enumerate(message.get("tool_calls") or [])]
        return LLMResponse(content=message.get("content") or "", tool_calls=calls)


def _to_openai(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}, **c.extra}
                             for c in m.tool_calls]
    return out


def _retry_after(response: httpx.Response) -> float | None:
    if response.headers.get("retry-after", "").isdigit():
        return float(response.headers["retry-after"])
    match = re.search(r"retry in ([\d.]+)s", response.text)
    return float(match.group(1)) + 1 if match else None


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def provider_from_env() -> LLMProvider:
    name = os.environ.get("LLM_PROVIDER", "groq").lower()
    if name == "fake":
        return ScriptedFakeProvider()
    if name == "groq":
        return OpenAICompatibleProvider("https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""),
                                        os.environ.get("LLM_MODEL", "openai/gpt-oss-120b"))
    if name == "openai":
        return OpenAICompatibleProvider(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                                        os.environ.get("OPENAI_API_KEY", ""), os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    if name == "gemini":
        return OpenAICompatibleProvider("https://generativelanguage.googleapis.com/v1beta/openai",
                                        os.environ.get("GEMINI_API_KEY", ""), os.environ.get("LLM_MODEL", "gemini-3.8-flash"))
    if name == "groq":
        return OpenAICompatibleProvider("https://api.groq.com/openai/v1", os.environ.get("GROQ_API_KEY", ""),
                                        os.environ.get("LLM_MODEL", "openai/gpt-oss-120b"))
    if name == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        return OpenAICompatibleProvider(base if base.endswith("/v1") else base + "/v1", "ollama", os.environ.get("LLM_MODEL", "llama3.1"))
    raise ValueError(f"Unknown LLM_PROVIDER {name!r}. Use fake, gemini, groq, openai or ollama.")


def extractor_for(provider: LLMProvider) -> Extractor:
    return null_extractor if isinstance(provider, ScriptedFakeProvider) else llm_extractor(provider)


# Metadata extraction: one forced tool call so the model can only answer in the schema.

EXTRACT_SYSTEM = """You extract metadata from an internal HR, payroll or time-management document.
Call record_metadata exactly once. Use only what the text states: title page, headers, footers, version or
revision blocks. The language is the language the text is written in. Any other field you cannot determine
from the text must be null. Never guess, and never infer from the filename alone."""

EXTRACT_TOOL: dict[str, Any] = {
    "name": "record_metadata",
    "description": "Record the document's metadata. Use null for anything the document does not state.",
    "input_schema": {
        "type": "object",
        "properties": {
            "language": {"type": ["string", "null"], "description": "ISO 639-1 code of the text's language, e.g. nl, fr, en"},
            "country": {"type": ["string", "null"], "description": "ISO 3166-1 alpha-2 code the document applies to, e.g. BE"},
            "department": {"type": ["string", "null"], "description": "Exactly one of HR, Payroll, Time"},
            "owner": {"type": ["string", "null"], "description": "Owning team or person as written in the document"},
            "created_at": {"type": ["string", "null"], "description": "Creation date as ISO 8601, e.g. 2024-03-01"},
            "updated_at": {"type": ["string", "null"], "description": "Last update or version date as ISO 8601"},
        },
        "required": list(FIELDS),
    },
}


def llm_extractor(provider: LLMProvider, max_chars: int = 12000) -> Extractor:
    def extract(filename: str, pages: list[str]) -> Metadata:
        sample = "\n\n".join(pages[:2] + (pages[-1:] if len(pages) > 2 else []))[:max_chars]
        response = provider.complete(EXTRACT_SYSTEM, [Message(role="user", content=f"Filename: {filename}\n\n{sample}")], [EXTRACT_TOOL])
        call = next((c for c in response.tool_calls if c.name == "record_metadata"), None)
        if call is None:
            return Metadata()
        return Metadata(**{field: clean(field, call.arguments.get(field)) for field in FIELDS})

    return extract
