"""ReAct loop guardrails, with a scripted LLM and fake tools (no database, no model)."""
import json

import pytest

from app.agent import react
from app.agent.tools import AgentContext, Tool
from app.chunking import Chunk
from app.generate import NOT_FOUND
from pydantic import BaseModel


class FakeConn:
    def __init__(self, has_graph=False):
        self.has_graph = has_graph

    def execute(self, *_):
        return self

    def fetchone(self):
        return (self.has_graph,)


class QueryArgs(BaseModel):
    query: str


def fake_search(ctx: AgentContext, args: QueryArgs) -> str:
    s, _ = ctx.register(Chunk(chunk_id=f"d:s1:c0", doc_id="d", section="1", text="Revenue was $17,455 million.",
                              context="Doc §1"), f"hybrid_search({args.query!r})")
    return f"[{s.n}] {s.chunk.context}\n{s.chunk.text}"


@pytest.fixture
def scripted(monkeypatch):
    """Replace tools with a fake search and the LLM with a script of replies; returns the calls log."""
    monkeypatch.setattr(react, "TOOLS", [Tool("hybrid_search", "(query)", "search", QueryArgs, fake_search),
                                         Tool("follow_links", "(source)", "graph", QueryArgs, fake_search, needs_graph=True)])
    calls = {"messages": [], "fallback": 0}

    def use(*replies):
        it = iter(replies)
        monkeypatch.setattr(react, "chat_messages", lambda messages, **k: calls["messages"].append(list(messages)) or next(it))

        def fallback(system, user, **k):
            calls["fallback"] += 1
            calls["synthesis_prompt"] = user
            return "Revenue was $17,455 million [1]."
        monkeypatch.setattr(react, "chat", fallback)
        return calls
    return use


def step(**kw):
    return json.dumps(kw)


def test_think_act_observe_then_answer(scripted):
    calls = scripted(step(thought="search", tool="hybrid_search", args={"query": "revenue"}),
                     step(thought="have it", final_answer="Revenue was $17,455 million [1]."))
    answer, ctx = react.run_agent(FakeConn(), "t", "What was revenue?")
    assert answer.answer == "Revenue was $17,455 million [1]."
    assert [c.source for c in answer.citations] == [1] and answer.citations[0].via == "hybrid_search('revenue')"
    assert [s["tool"] for s in answer.steps] == ["hybrid_search", None]
    assert "Observation:" in calls["messages"][1][-1]["content"]


def test_graph_tools_hidden_without_a_graph(scripted):
    calls = scripted(step(thought="x", final_answer=NOT_FOUND))
    react.run_agent(FakeConn(has_graph=False), "t", "q")
    assert "follow_links" not in calls["messages"][0][0]["content"]
    calls = scripted(step(thought="x", final_answer=NOT_FOUND))
    react.run_agent(FakeConn(has_graph=True), "t", "q")
    assert "follow_links" in calls["messages"][-1][0]["content"]


def test_invalid_json_and_unknown_tool_get_error_observations(scripted):
    scripted("not json",
             step(thought="ok", tool="hybrid_search", args={"query": "revenue"}),   # recovers: error count resets
             step(thought="x", tool="nope", args={}),
             step(thought="done", final_answer="Revenue was $17,455 million [1]."))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    errors = [s["error"] for s in answer.steps if s["error"]]
    assert len(errors) == 2 and "not valid JSON" in errors[0] and "unknown tool" in errors[1]
    assert answer.citations


def test_two_consecutive_errors_end_the_loop(scripted):
    calls = scripted("bad", "still bad", step(thought="final", final_answer=NOT_FOUND))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    assert answer.answer == NOT_FOUND and len(calls["messages"]) == 3   # 2 failed steps + forced final turn


def test_repeated_call_is_refused(scripted):
    same = step(thought="again", tool="hybrid_search", args={"query": "revenue"})
    scripted(same, same, step(thought="done", final_answer="Revenue was $17,455 million [1]."))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    assert "already made exactly this call" in answer.steps[1]["observation"]


def test_step_limit_forces_an_answer(scripted):
    loop = [step(thought="more", tool="hybrid_search", args={"query": f"q{i}"}) for i in range(3)]
    scripted(*loop, step(thought="forced", final_answer="Revenue was $17,455 million [1]."))
    answer, _ = react.run_agent(FakeConn(), "t", "q", max_steps=3)
    assert answer.answer.endswith("[1].") and "step limit" in answer.steps[-1]["thought"]


def test_unobserved_citations_are_stripped(scripted):
    scripted(step(thought="s", tool="hybrid_search", args={"query": "revenue"}),
             step(thought="d", final_answer="Revenue was $17,455 million [1][7]."))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    assert answer.answer == "Revenue was $17,455 million [1]." and [c.source for c in answer.citations] == [1]


def test_final_answer_is_synthesized_from_full_sources_with_the_draft(scripted):
    calls = scripted(step(thought="s", tool="hybrid_search", args={"query": "revenue"}),
                     step(thought="d", final_answer="Revenue was big [1]."))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    prompt = calls["synthesis_prompt"]
    assert calls["fallback"] == 1 and answer.citations
    assert "[1] Doc §1 (found by hybrid_search('revenue'))" in prompt   # trace numbering kept
    assert "Revenue was $17,455 million." in prompt                     # full text, not a snippet
    assert "Revenue was big [1]." in prompt                             # the agent's draft, as a hint


def test_answer_without_any_sources_becomes_not_found(scripted):
    scripted(step(thought="I just know", final_answer="Revenue was $1 trillion."))
    answer, _ = react.run_agent(FakeConn(), "t", "q")
    assert answer.answer == NOT_FOUND and not answer.citations
