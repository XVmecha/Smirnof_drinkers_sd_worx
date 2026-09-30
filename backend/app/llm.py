"""Answer + conflict detection via OpenAI-compatible chat APIs (Gemini, Groq).

Providers are tried in order (LLM_ORDER), and each provider's models in order:
the first model that answers wins. See the README for why Groq is the backup.
"""

import json
import logging
import re
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from .knowledge import Hit

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

SYSTEM_PROMPT = """You are a payroll knowledge assistant for payroll consultants.
Answer ONLY from the numbered sources below. Each source lists its metadata: country,
joint committees, document type, owner, last updated date and whether it was replaced.

Rules:
- Prefer sources that are current, have an owner, are official (policy/annex) and match the
  client's country and joint committee.
- Do not base the answer on a source for another country or one that was replaced. If such a
  source says something different, mention it in one short sentence.
- Cite every claim with the source number in square brackets, e.g. [1].
- If the sources do not answer the question, say so plainly. Never invent rules or numbers.
- Keep the answer under 150 words, in the language of the question.

Also report conflicts: pairs of sources that make contradicting claims relevant to the
question (e.g. different numbers, caps, dates or rules for the same thing), where BOTH sources
apply to this client (right country, not replaced). Informal sources such as chat messages,
emails and drafts DO count: a colleague's chat message contradicting a policy is exactly the
kind of conflict the consultant must see. Mention such a conflict in the answer too.

Reply with JSON only:
{"answer": "<markdown answer with [n] citations>",
 "conflicts": [{"a": <n>, "b": <n>, "topic": "<few words>",
                "a_says": "<short quote from a>", "b_says": "<short quote from b>"}]}"""


def build_context(hits: list[Hit]) -> str:
    blocks = []
    for n, hit in enumerate(hits, start=1):
        m = hit.doc.meta
        meta = ", ".join(
            f"{label}: {m[key]}"
            for key, label in (
                ("country", "country"),
                ("joint_committees", "joint committees"),
                ("doc_type", "type"),
                ("owner", "owner"),
                ("updated", "last updated"),
                ("superseded_by", "REPLACED BY"),
            )
            if m.get(key)
        )
        title = m.get("title") or hit.doc.filename
        blocks.append(f"[{n}] {title}\n({meta or 'no metadata'})\n{hit.excerpt}")
    return "\n\n---\n\n".join(blocks)


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
                        temperature=0.2,
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

    async def answer(self, question: str, client: dict, history: list[dict], hits: list[Hit]) -> dict:
        client_line = (
            f"Client: {client.get('name') or client.get('id')}, country {client.get('country') or 'unknown'}"
            + (f", joint committee {client['joint_committee']}" if client.get("joint_committee") else "")
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history[-6:],
            {"role": "user", "content": f"{client_line}\n\nSources:\n\n{build_context(hits)}\n\nQuestion: {question}"},
        ]
        content, used = await self._complete(messages)
        data = parse_json(content)

        # Map source numbers back to ids and drop anything malformed.
        conflicts = []
        for c in data.get("conflicts") or []:
            try:
                a, b = hits[int(c["a"]) - 1].doc.id, hits[int(c["b"]) - 1].doc.id
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            if a != b:
                conflicts.append(
                    {"a": a, "b": b, "topic": str(c.get("topic", "")), "a_says": str(c.get("a_says", "")),
                     "b_says": str(c.get("b_says", ""))}
                )
        return {"answer": str(data.get("answer") or "").strip(), "conflicts": conflicts, "model": used}
