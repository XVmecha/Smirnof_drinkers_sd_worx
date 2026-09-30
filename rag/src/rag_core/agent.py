"""Provider-neutral LLM interface, a scripted fake, and the bounded agent loop."""
from __future__ import annotations

import json
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .search import DOC_LIMIT, READ_PAGES_MAX, TOOLS, Index

log = logging.getLogger(__name__)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)  # provider-specific fields to echo back, e.g. Gemini's thought signature


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # set when role == "tool"


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLMProvider(Protocol):
    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]] | None) -> LLMResponse:
        """One model turn. `tools=None` means tool use is not allowed and a final answer is expected."""
        ...


SYSTEM_PROMPT = """You are a knowledge assistant for SD Worx employees in HR, payroll and time management.
You answer questions using only the internal documents reachable through your tools.

Tools
- search(query): full-text search over all documents. Returns up to {doc_limit} documents, each with a ref
  number, doc_id, filename, metadata, provenance and passages (matching body text with page numbers).
- read_pages(doc_id, pages): the full text of up to {read_max} pages of one document, to read around a
  passage or to verify what it says.

Metadata
Every document has six metadata fields: language, country, department (HR, Payroll or Time), owner,
created_at, updated_at. A field can be null, which means the document does not state it. Never guess or
fill in a missing value. Provenance says where each field came from: "sidecar" (supplied with the file),
"llm" (extracted from the document text) or "missing".

How to work
1. Search with the user's question. Rephrase and search again if the results do not answer it.
2. Read pages when a passage is cut off or when you need to confirm what the document says.
3. You may make at most {max_tool_calls} tool calls in total. Stop as soon as you have enough.

How to answer
- Answer in the language of the question, in markdown.
- Base every statement on document text and cite the document as [ref] using its ref number.
  If the documents do not answer the question, say so plainly.
- After the answer, list every document you relied on: [ref] filename, then all six metadata fields
  (write "unknown" where the value is null) with the provenance of each field.
- Say explicitly when relevant metadata is unknown, when a document may be outdated, or when documents
  disagree with each other.
"""

BUDGET_EXHAUSTED = "You have used all your tool calls. Answer now with what you have and say that the tool budget was exhausted."
NO_ANSWER = "The agent produced no answer."


class AgentResult(BaseModel):
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)  # every document seen in search results, in ref order
    trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls_used: int = 0
    hit_cap: bool = False


def run_agent(question: str, index: Index, provider: LLMProvider, max_tool_calls: int = 8) -> AgentResult:
    system = SYSTEM_PROMPT.format(doc_limit=DOC_LIMIT, read_max=READ_PAGES_MAX, max_tool_calls=max_tool_calls)
    messages = [Message(role="user", content=question)]
    sources: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    used = 0

    # Every turn either ends the loop or issues a tool call, so turns are bounded by the budget plus the forced final turn.
    for _ in range(max_tool_calls + 2):
        tools = TOOLS if used < max_tool_calls else None
        response = provider.complete(system, messages, tools)
        if tools is None or not response.tool_calls:
            return AgentResult(answer=response.content.strip() or NO_ANSWER, sources=list(sources.values()),
                               trace=trace, tool_calls_used=used, hit_cap=tools is None)

        messages.append(Message(role="assistant", content=response.content, tool_calls=response.tool_calls))
        for call in response.tool_calls:
            if used < max_tool_calls:
                used += 1
                result = index.run_tool(call.name, call.arguments)
                for doc in result.get("documents", []):
                    # Stable ref number per document for citations, assigned in order of first appearance.
                    doc["ref"] = sources.setdefault(doc["doc_id"], {**doc, "ref": len(sources) + 1})["ref"]
                trace.append({"step": used, "tool": call.name, "arguments": call.arguments, "result": result})
                log.info("tool %d/%d %s %s", used, max_tool_calls, call.name, json.dumps(call.arguments, ensure_ascii=False))
            else:
                result = {"error": "tool budget exhausted"}
            messages.append(Message(role="tool", tool_call_id=call.id, content=json.dumps(result, ensure_ascii=False)))
        if used >= max_tool_calls:
            messages.append(Message(role="user", content=BUDGET_EXHAUSTED))

    return AgentResult(answer=NO_ANSWER, sources=list(sources.values()), trace=trace, tool_calls_used=used, hit_cap=True)


class ScriptedFakeProvider:
    """Deterministic stand-in for a model: search with the question, open the first hit's page, then answer."""

    def complete(self, system: str, messages: list[Message], tools: list[dict[str, Any]] | None) -> LLMResponse:
        question = next((m.content for m in messages if m.role == "user"), "")
        results = [json.loads(m.content) for m in messages if m.role == "tool"]
        documents = next((r["documents"] for r in results if "documents" in r), [])

        if tools is not None and not results:
            return LLMResponse(tool_calls=[ToolCall(id="call_1", name="search", arguments={"query": question})])
        if tools is not None and len(results) == 1 and documents:
            first = documents[0]
            return LLMResponse(tool_calls=[ToolCall(
                id="call_2", name="read_pages", arguments={"doc_id": first["doc_id"], "pages": [first["passages"][0]["page"]]})])
        if not documents:
            return LLMResponse(content="No documents matched the question.")

        lines = [f"Found {len(documents)} document(s) for: {question}", ""]
        for doc in documents:
            meta = ", ".join(f"{k}={'unknown' if v is None else v} ({doc['provenance'][k]})" for k, v in doc["metadata"].items())
            excerpt = doc["passages"][0]["text"].replace("\n", " ").strip()[:200]
            lines.append(f"- [{doc['ref']}] {doc['filename']}: {meta}")
            lines.append(f"  page {doc['passages'][0]['page']}: {excerpt}")
        if tools is None:
            lines += ["", "Tool budget exhausted; answered with the results gathered so far."]
        return LLMResponse(content="\n".join(lines))

