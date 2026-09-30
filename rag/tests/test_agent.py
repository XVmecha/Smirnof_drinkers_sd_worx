from conftest import SequenceProvider

from rag_core.agent import NO_ANSWER, LLMResponse, ScriptedFakeProvider, ToolCall, run_agent


def search_call(i: int, query: str) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="search", arguments={"query": query})])


def test_fake_provider_end_to_end(index):
    result = run_agent("Hoeveel vakantiedagen krijg ik?", index, ScriptedFakeProvider())
    assert [t["tool"] for t in result.trace] == ["search", "read_pages"]
    assert result.tool_calls_used == 2 and result.hit_cap is False
    assert "[1] vakantiebeleid.pdf" in result.answer and "country=BE (sidecar)" in result.answer
    assert [s["ref"] for s in result.sources] == [1] and result.sources[0]["filename"] == "vakantiebeleid.pdf"


def test_fake_provider_no_results(index):
    result = run_agent("zzzz qqqq", index, ScriptedFakeProvider())
    assert result.tool_calls_used == 1 and result.answer == "No documents matched the question." and result.sources == []


def test_refs_are_stable_across_searches(index):
    provider = SequenceProvider([search_call(1, "vakantiedagen"), search_call(2, "timesheet"), search_call(3, "vakantiedagen")])
    result = run_agent("q", index, provider)
    assert [(s["ref"], s["filename"]) for s in result.sources] == [(1, "vakantiebeleid.pdf"), (2, "timesheet_rules.pdf")]
    assert result.trace[2]["result"]["documents"][0]["ref"] == 1  # same document, same ref the second time


def test_tool_budget_is_enforced(index):
    greedy = SequenceProvider([search_call(i, "timesheet") for i in range(50)])
    result = run_agent("anything", index, greedy, max_tool_calls=8)
    assert result.tool_calls_used == 8 and result.hit_cap is True
    assert greedy.calls[-1]["tools"] is None  # forced final turn without tools
    assert result.answer == NO_ANSWER  # its tool call was ignored and it gave no text


def test_forced_final_answer_after_budget(index):
    provider = SequenceProvider([search_call(i, "prime") for i in range(8)] + [LLMResponse(content="Budget hit.")])
    result = run_agent("prime?", index, provider, max_tool_calls=8)
    assert result.hit_cap is True and result.answer == "Budget hit." and len(result.trace) == 8


def test_multiple_tool_calls_in_one_turn(index):
    provider = SequenceProvider([
        LLMResponse(tool_calls=[ToolCall(id="a", name="search", arguments={"query": "vakantie"}),
                                ToolCall(id="b", name="search", arguments={"query": "timesheet"})]),
        LLMResponse(content="two searches done"),
    ])
    result = run_agent("q", index, provider)
    assert result.tool_calls_used == 2 and result.answer == "two searches done"
    assert [m.tool_call_id for m in provider.calls[1]["messages"] if m.role == "tool"] == ["a", "b"]
