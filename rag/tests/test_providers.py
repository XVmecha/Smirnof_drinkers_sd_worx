import json

import httpx
from conftest import SequenceProvider

from rag_core.agent import LLMResponse, Message, ToolCall
from rag_core.ingest import ingest
from rag_core.models import FIELDS
from rag_core.providers import EXTRACT_TOOL, OpenAICompatibleProvider, llm_extractor
from rag_core.search import TOOLS


def make_provider(reply: dict, seen: dict) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["auth"], seen["url"] = json.loads(request.content), request.headers["authorization"], str(request.url)
        return httpx.Response(200, json={"choices": [{"message": reply}]})
    return OpenAICompatibleProvider("http://test/v1", "sk-test", "m", transport=httpx.MockTransport(handler))


def test_openai_adapter_translates_messages_tools_and_tool_calls():
    seen = {}
    provider = make_provider({"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"query": "overtime"}'},
         "extra_content": {"google": {"thought_signature": "sig"}}}]}, seen)
    messages = [Message(role="user", content="q"),
                Message(role="assistant", tool_calls=[ToolCall(id="a", name="search", arguments={"query": "x"}, extra={"extra_content": {"k": 1}})]),
                Message(role="tool", tool_call_id="a", content="{}")]
    response = provider.complete("sys", messages, TOOLS)

    body = seen["body"]
    assert seen["url"] == "http://test/v1/chat/completions" and seen["auth"] == "Bearer sk-test" and body["model"] == "m"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"query": "x"}'}
    assert body["messages"][2]["tool_calls"][0]["extra_content"] == {"k": 1}  # provider extras are echoed back
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "a", "content": "{}"}
    assert [t["function"]["name"] for t in body["tools"]] == ["search", "read_pages"]
    assert body["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert response.content == "" and response.tool_calls == [ToolCall(id="call_1", name="search", arguments={"query": "overtime"},
                                                                       extra={"extra_content": {"google": {"thought_signature": "sig"}}})]


def test_openai_adapter_omits_tools_on_forced_final_turn():
    seen = {}
    response = make_provider({"role": "assistant", "content": "final"}, seen).complete("sys", [Message(role="user", content="q")], None)
    assert "tools" not in seen["body"] and response.content == "final" and response.tool_calls == []


def test_llm_extractor_cleans_values_and_never_guesses():
    provider = SequenceProvider([LLMResponse(tool_calls=[ToolCall(id="1", name="record_metadata", arguments={
        "language": "nl", "country": "BE", "department": "finance", "owner": " ", "created_at": None, "updated_at": "2025-06-01"})])])
    meta = llm_extractor(provider)("x.pdf", ["page one", "page two", "page three", "last page"])
    assert meta.model_dump() == {"language": "nl", "country": "BE", "department": None, "owner": None, "created_at": None, "updated_at": "2025-06-01"}
    call = provider.calls[0]
    assert call["tools"] == [EXTRACT_TOOL]
    assert "page one" in call["messages"][0].content and "last page" in call["messages"][0].content and "page three" not in call["messages"][0].content


def test_llm_extractor_without_tool_call_returns_nothing():
    assert llm_extractor(SequenceProvider([LLMResponse(content="It is HR, I think")]))("x.pdf", ["t"]).model_dump() == {f: None for f in FIELDS}


def test_extractor_failure_is_an_ingest_error_not_missing_metadata(settings):
    class Broken:
        def complete(self, *a):
            raise RuntimeError("provider down")
    docs = {}
    report = ingest(settings, docs, llm_extractor(Broken()))
    assert report.ingested == ["hr/vakantiebeleid.pdf"]  # complete sidecar, extractor never called
    assert set(report.errors) == {"paie_primes.pdf", "timesheet_rules.pdf"} and "provider down" in report.errors["paie_primes.pdf"]
    assert len(docs) == 1  # failed documents are not stored, so the next ingest retries them


def test_openai_adapter_retries_transient_errors():
    codes = iter([503, 429, 200])
    def handler(request):
        code = next(codes)
        return httpx.Response(code, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]} if code == 200 else {"error": "busy"})
    provider = OpenAICompatibleProvider("http://test/v1", "k", "m", transport=httpx.MockTransport(handler))
    import rag_core.providers as mod
    mod.time.sleep = lambda s: None
    assert provider.complete("sys", [Message(role="user", content="q")], None).content == "ok"


def test_ingest_calls_extractor_only_for_missing_fields_and_keeps_sidecar_precedence(settings):
    def record(**fields):
        return LLMResponse(tool_calls=[ToolCall(id="r", name="record_metadata", arguments={f: fields.get(f) for f in FIELDS})])
    # Sorted ingestion order: vakantiebeleid (complete sidecar, no call), paie_primes (partial), timesheet_rules (none).
    provider = SequenceProvider([record(language="fr", country="BE", department="Payroll"),
                                 record(language="en", department="Time", owner="Time Team")])
    docs = {}
    ingest(settings, docs, llm_extractor(provider))
    by_name = {d.filename: d for d in docs.values()}

    assert len(provider.calls) == 2
    paie = by_name["paie_primes.pdf"]
    assert paie.metadata.country == "FR" and paie.provenance["country"] == "sidecar"  # sidecar wins over the model's BE
    assert paie.metadata.language == "fr" and paie.provenance["language"] == "llm"
    assert paie.metadata.department == "Payroll" and paie.provenance["department"] == "llm"  # sidecar had invalid "Finance"
    assert paie.provenance["updated_at"] == "missing"
    ts = by_name["timesheet_rules.pdf"]
    assert ts.metadata.model_dump() == {"language": "en", "country": None, "department": "Time", "owner": "Time Team", "created_at": None, "updated_at": None}
    assert ts.provenance == {"language": "llm", "country": "missing", "department": "llm", "owner": "llm", "created_at": "missing", "updated_at": "missing"}
