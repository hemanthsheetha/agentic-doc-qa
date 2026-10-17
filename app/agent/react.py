"""ReAct loop: think -> pick a tool -> observe -> repeat, at most MAX_STEPS tool steps, then a grounded answer.

Each LLM turn is one JSON object (Ollama JSON mode):
  {"thought": "...", "tool": "hybrid_search", "args": {"query": "..."}}    -> run the tool, observe
  {"thought": "...", "final_answer": "... [3][5]"}                          -> done
Guardrails: invalid JSON / unknown tool / bad args get an error observation (two in a row end the loop);
a repeated identical call is refused; at the step limit the agent must answer from what it observed.
Grounding: the answer may cite only observed sources; invalid [n] are stripped, and an answer citing
nothing is regenerated from the observed sources by the standard cited-answer prompt.
"""
import json
import logging
import re
import time
from typing import Callable

import psycopg
from pydantic import BaseModel

from app.agent.tools import TOOLS, AgentContext
from app.generate import NOT_FOUND, SYSTEM_PROMPT as ANSWER_PROMPT, Answer, build_citations
from app.llm import chat, chat_messages

logger = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_CONSECUTIVE_ERRORS = 2
OBSERVATION_CHARS = 3000     # per step, keeps 8 steps inside the 8k context


class AgentStep(BaseModel):
    step: int
    thought: str = ""
    tool: str | None = None
    args: dict = {}
    observation: str = ""
    error: str | None = None
    seconds: float = 0.0


def system_prompt(tools: list, max_steps: int) -> str:
    tool_lines = "\n".join(f"- {t.name}{t.signature}: {t.description}" for t in tools)
    graph_rule = ("\n- When a source points to something you don't have yet (\"see X §N\", a role without a name, "
                  "a tier or severity whose rules you need), use follow_links or lookup_entity instead of guessing."
                  "\n- A role or team (\"the Data Platform lead\", \"the owning team's lead\") is not a person's name. "
                  "Observations list what is resolvable in the context graph; use those lookups."
                  if any(t.needs_graph for t in tools) else "")
    return f"""You are a research agent. You answer one question about a workspace's documents by using tools.
Work step by step. Reply with exactly ONE JSON object per turn, either a tool call:
  {{"thought": "<what you know so far and what you still need>", "tool": "<tool name>", "args": {{...}}}}
or, once the observed sources answer the question:
  {{"thought": "<why the sources are enough>", "final_answer": "<the answer, citing sources like [3]>"}}

Tools:
{tool_lines}

Rules:
- Start with hybrid_search.{graph_rule}
- Before a final_answer, check every part of the question: if any part (for example a person's name or an
  exact number) is not stated in an observed source yet, make another tool call instead of answering.
- Every sentence of the final answer cites the observed sources it relies on, e.g. [2][5].
  Never cite a number you have not seen.
- Quote figures and names exactly as the sources state them.
- If the sources do not answer the question, final_answer must be exactly: "{NOT_FOUND}"
- You have at most {max_steps} tool calls. Never repeat the same call.
- Use a markdown table only when comparing several figures; otherwise answer in short prose."""


def _parse(raw: str) -> dict:
    try:
        step = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e.msg})") from e
    if not isinstance(step, dict):
        raise ValueError("reply must be a JSON object")
    if not step.get("final_answer") and not step.get("tool"):
        raise ValueError('reply needs either "tool" (with "args") or "final_answer"')
    return step


def _ground(text: str, ctx: AgentContext) -> tuple[str, int]:
    """Strip citations to sources the agent never observed. Returns (text, number of valid citations)."""
    valid = 0

    def keep(m):
        nonlocal valid
        if 1 <= int(m.group(1)) <= len(ctx.sources):
            valid += 1
            return m.group(0)
        logger.warning("agent cited unobserved source %s; stripped", m.group(0))
        return ""

    return re.sub(r"\[(\d+)\]", keep, text).strip(), valid


SYNTHESIS_SOURCES = 10
SYNTHESIS_SOURCE_CHARS = 1800


def synthesize(ctx: AgentContext, question: str, draft: str) -> str:
    """Final answer over the FULL text of the sources the agent gathered.

    During the loop the agent only sees short snippets (to fit 8 steps in the context window), so the fact it
    needs can sit just past a cut. The agent is the researcher; this step writes the answer, with the agent's
    draft as a hint to verify. Sources keep their trace numbers: [6] here is the [6] the agent found.
    """
    cited = [int(n) for n in re.findall(r"\[(\d+)\]", draft) if 1 <= int(n) <= len(ctx.sources)]
    picked = list(dict.fromkeys(cited + [s.n for s in ctx.sources]))[:SYNTHESIS_SOURCES]
    block = "\n\n".join(
        f"[{n}] {ctx.source(n).chunk.context} (found by {ctx.source(n).found_by})\n"
        f"{ctx.source(n).chunk.text[:SYNTHESIS_SOURCE_CHARS]}" for n in sorted(picked))
    hint = (f"\n\nA research agent drafted this answer from short snippets of these sources. Use it as a hint, "
            f"verify every claim against the full text above, and correct or complete it:\n{draft}"
            if draft and draft.strip() != NOT_FOUND else "")
    text, valid = _ground(chat(ANSWER_PROMPT, f"Sources:\n\n{block}{hint}\n\nQuestion: {question}"), ctx)
    if not valid and text.strip() != NOT_FOUND:
        logger.warning("synthesized answer cites no observed source: %r", text[:200])
    return text


def run_agent(
    conn: psycopg.Connection, tenant_id: str, question: str, doc_ids: list[str] | None = None,
    max_steps: int = MAX_STEPS, on_step: Callable[[list[AgentStep]], None] | None = None,
) -> tuple[Answer, AgentContext]:
    """Answer with the ReAct loop. Returns the answer and the context (observed sources), e.g. for evals."""
    start = time.perf_counter()
    ctx = AgentContext(conn, tenant_id, question, doc_ids)
    has_graph = conn.execute("SELECT EXISTS (SELECT 1 FROM graph_nodes WHERE tenant_id = %s)", (tenant_id,)).fetchone()[0]
    tools = {t.name: t for t in TOOLS if has_graph or not t.needs_graph}
    messages = [{"role": "system", "content": system_prompt(list(tools.values()), max_steps)},
                {"role": "user", "content": f"Question: {question}"}]
    steps: list[AgentStep] = []
    seen_calls: set[str] = set()
    final, errors = None, 0

    for n in range(1, max_steps + 1):
        t0 = time.perf_counter()
        raw = chat_messages(messages, json_mode=True)
        messages.append({"role": "assistant", "content": raw})
        step = AgentStep(step=n)
        try:
            parsed = _parse(raw)
            step.thought = str(parsed.get("thought", ""))
            if parsed.get("final_answer"):
                final = str(parsed["final_answer"])
                step.seconds = round(time.perf_counter() - t0, 1)
                steps.append(step)
                break
            step.tool, step.args = str(parsed["tool"]), parsed.get("args") or {}
            if step.tool not in tools:
                raise ValueError(f"unknown tool {step.tool!r}; available: {', '.join(tools)}")
            call_key = f"{step.tool}:{json.dumps(step.args, sort_keys=True)}"
            if call_key in seen_calls:
                observation = "You already made exactly this call; its results are above. Use them or try something else."
            else:
                seen_calls.add(call_key)
                observation = tools[step.tool].call(ctx, step.args)
            errors = 0
        except ValueError as e:
            errors += 1
            step.error = str(e)
            observation = f"Error: {e}. Reply with one JSON object as specified."
        step.observation = observation[:OBSERVATION_CHARS]
        step.seconds = round(time.perf_counter() - t0, 1)
        steps.append(step)
        logger.info("agent step %d: tool=%s args=%s error=%s (%.1fs)", n, step.tool, step.args, step.error, step.seconds)
        if on_step:
            on_step(steps)
        if errors >= MAX_CONSECUTIVE_ERRORS:
            break
        messages.append({"role": "user", "content": f"Observation:\n{step.observation}\n\n(step {n} of {max_steps})"})

    if final is None:   # step limit or repeated errors: one last turn to answer from what was observed
        messages.append({"role": "user", "content": 'No more tool calls. Reply now with {"thought": "...", '
                                                    '"final_answer": "..."} using only the sources above.'})
        try:
            final = str(_parse(chat_messages(messages, json_mode=True)).get("final_answer") or "") or None
        except ValueError:
            final = None
        steps.append(AgentStep(step=len(steps) + 1, thought="(step limit reached: answering from observed sources)"))

    chunks = [s.chunk for s in ctx.sources]
    vias = [s.found_by for s in ctx.sources]
    draft, _ = _ground(final or "", ctx)
    text = synthesize(ctx, question, draft) if chunks else NOT_FOUND
    logger.info("agent answered in %d steps, %d sources observed, %.1fs", len(steps), len(chunks),
                time.perf_counter() - start)
    if on_step:
        on_step(steps)
    return Answer(answer=text, citations=build_citations(text, chunks, vias), strategy="agent",
                  steps=[s.model_dump() for s in steps]), ctx
