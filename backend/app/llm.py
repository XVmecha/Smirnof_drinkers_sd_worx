"""The relevance call (see relevance.py) via OpenAI-compatible chat APIs (Groq, Gemini).

Providers are tried in order (LLM_ORDER), and each provider's models in order:
the first model that answers wins. See the README for why Groq is the backup.
"""

import json
import logging
import re
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

log = logging.getLogger(__name__)
# Errors worth trying the next model for: overloaded, rate limited, retired model.
NEXT_MODEL_STATUSES = {400, 404, 408, 429, 500, 502, 503, 504}
# Errors that mean this provider is unusable (bad or missing key): skip to the next provider.
NEXT_PROVIDER_STATUSES = {401, 403}


@dataclass
class Provider:
    name: str
    client: AsyncOpenAI
    models: list[str]

    @classmethod
    def create(cls, name: str, base_url: str, api_key: str, models: list[str], timeout: float) -> "Provider":
        # No SDK retries: falling through to the next model is faster than waiting on an overloaded one.
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)
        return cls(name=name, client=client, models=[m.strip() for m in models if m.strip()])

def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        data = json.loads(match.group(0)) if match else {"answer": text}
    return data if isinstance(data, dict) else {"answer": str(data)}


class LLM:
    def __init__(self, providers: list[Provider]):
        self.providers = [p for p in providers if p.models]

    @property
    def chain(self) -> list[str]:
        return [f"{p.name}/{m}" for p in self.providers for m in p.models]

    async def _complete(self, messages: list[dict]) -> tuple[str, str]:
        """Try each provider/model in order; return (content, 'provider/model')."""
        if not self.providers:
            raise RuntimeError("no LLM provider configured")
        last_error: Exception | None = None
        for provider in self.providers:
            for model in provider.models:
                try:
                    resp = await provider.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0,
                        response_format={"type": "json_object"},
                    )
                    return resp.choices[0].message.content or "", f"{provider.name}/{model}"
                except APIStatusError as e:
                    last_error = e
                    log.warning("%s/%s failed with HTTP %s", provider.name, model, e.status_code)
                    if e.status_code in NEXT_PROVIDER_STATUSES:
                        break
                    if e.status_code not in NEXT_MODEL_STATUSES:
                        raise
                except APIConnectionError as e:  # includes timeouts
                    last_error = e
                    log.warning("%s/%s unreachable (%s)", provider.name, model, type(e).__name__)
        raise last_error

    async def describe(self, prompt: str, question: str, main: list[str], other: list[str]) -> dict:
        """One call with the relevance prompt. The model orders and describes; the code keeps the facts:
        ids it was not shown are dropped, each id appears once, and the main/other split stays the code's."""
        content, used = await self._complete([{"role": "system", "content": prompt}, {"role": "user", "content": question}])
        data = parse_json(content)
        shown = set(main) | set(other)

        def entries(key: str, text_key: str) -> list[dict]:
            out = []
            for item in data.get(key) or []:
                if isinstance(item, dict) and str(item.get("id")) in shown:
                    out.append({"id": str(item["id"]), text_key: str(item.get(text_key) or "").strip()})
            return out

        suspicious = {e["id"]: e for e in entries("suspicious", "reason")}
        seen: set[str] = set(suspicious)
        ranked = []
        for e in entries("ranking", "notable") + entries("other_country", "notable"):
            if e["id"] not in seen:
                seen.add(e["id"])
                ranked.append(e)
        notes = [str(n).strip() for n in data.get("cross_document_notes") or [] if str(n).strip()][:3]
        return {
            "answer": str(data.get("answer") or "").strip(),
            "ranking": [e for e in ranked if e["id"] in main],
            "other_country": [e for e in ranked if e["id"] in other],
            "suspicious": list(suspicious.values()),
            "notes": notes,
            "model": used,
        }
