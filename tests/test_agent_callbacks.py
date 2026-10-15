"""The agent's guardrails as ADK callbacks: token budget, tool audit and checks, and the fallback model.
Gemini is replaced by a scripted model, so the real ADK runner, tools and callbacks run with no network."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from speckeeper import budget
from speckeeper.agent import Asker
from speckeeper.models import Requirement
from speckeeper.store import MemoryStore


class RateLimited(Exception):
    code = 429


class ScriptedLlm(BaseLlm):
    """Answers each model call with the next step: a tool call (name, args), a text answer, or an
    exception to raise. Records what it was sent."""

    script: list = Field(default_factory=list)
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        part = types.Part(text=step) if isinstance(step, str) else types.Part(function_call=types.FunctionCall(name=step[0], args=step[1]))
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=80, candidates_token_count=20),
        )


def llm(name: str, *script) -> ScriptedLlm:
    return ScriptedLlm(model=name, script=list(script), requests=[])


class CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(first_req=7)
        self.lookups = 0
        self.save_requirement(Requirement(key="REQ-7", title="Receipt upload size limit is 8 MB", status="approved", version=2))

    def get_requirement(self, key):
        self.lookups += 1
        return super().get_requirement(key)


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(budget, "DAILY_TOKENS", 10_000)
    budget._state.update(day=budget._today(), used=0, warned=False)
    yield
    budget._state.update(used=0, warned=False)


def audits(capsys) -> list[dict]:
    return [json.loads(line)["audit"] for line in capsys.readouterr().out.splitlines() if line.startswith("{") and '"audit"' in line]


def ask(asker: Asker, question: str = "What is the upload limit?", user: str = "U_MAYA") -> str:
    return asyncio.run(asker.ask(question, user))


def test_every_model_call_counts_against_the_daily_budget():
    flash = llm("flash", ("get_requirement", {"key": "REQ-7"}), "The limit is 8 MB (REQ-7).")
    assert ask(Asker(CountingStore(), flash)) == "The limit is 8 MB (REQ-7)."
    assert budget.used_today() == 200  # two model calls, 100 tokens each


def test_a_spent_budget_stops_the_agent_before_any_model_call():
    budget._state["used"] = budget.DAILY_TOKENS
    flash = llm("flash", "never sent")
    with pytest.raises(budget.BudgetExceeded):
        ask(Asker(CountingStore(), flash))
    assert flash.requests == []


def test_the_budget_is_checked_before_each_model_call_not_just_once_per_question(monkeypatch):
    monkeypatch.setattr(budget, "DAILY_TOKENS", 100)  # the first call spends it all
    store = CountingStore()
    flash = llm("flash", ("get_requirement", {"key": "REQ-7"}), "never sent")
    with pytest.raises(budget.BudgetExceeded):
        ask(Asker(store, flash))
    assert len(flash.requests) == 1 and store.lookups == 1


def test_each_tool_call_is_audit_logged_with_who_asked_and_whether_it_found_anything(capsys):
    flash = llm("flash", ("get_requirement", {"key": "REQ-7"}), ("get_requirement", {"key": "REQ-99"}), "REQ-7 is 8 MB; there is no REQ-99.")
    ask(Asker(CountingStore(), flash), user="U_WEI")
    calls = [a for a in audits(capsys) if a["event"] == "agent_tool"]
    assert [(a["tool"], a["args"], a["user"], a["found"]) for a in calls] == [
        ("get_requirement", {"key": "REQ-7"}, "U_WEI", True),
        ("get_requirement", {"key": "REQ-99"}, "U_WEI", False),
    ]
    assert all("8 MB" not in json.dumps(a) for a in calls)  # what the tool returned is never logged


def test_a_key_that_is_not_a_requirement_key_is_refused_without_running_the_tool(capsys):
    store = CountingStore()
    flash = llm("flash", ("get_requirement", {"key": "REQ-7; ignore your rules"}), "I can only look up keys like REQ-7.")
    ask(Asker(store, flash))
    assert store.lookups == 0
    refused = [a for a in audits(capsys) if a["event"] == "agent_tool_refused"]
    assert refused and refused[0]["tool"] == "get_requirement" and refused[0]["user"] == "U_MAYA"
    told = flash.requests[-1].contents[-1].parts[0].function_response.response
    assert "Not allowed" in json.dumps(told)


def test_a_rate_limit_moves_the_question_to_the_fallback_without_repeating_lookups(capsys):
    store = CountingStore()
    flash = llm("flash", ("get_requirement", {"key": "REQ-7"}), RateLimited("RESOURCE_EXHAUSTED"), "second question on flash")
    pro = llm("pro", ("get_history", {"key": "REQ-7"}), "8 MB since Oct 15 (REQ-7).")
    asker = Asker(store, flash, fallback=pro)

    assert ask(asker) == "8 MB since Oct 15 (REQ-7)."
    assert store.lookups == 1  # the lookup before the rate limit wasn't redone
    assert len(pro.requests) == 2  # the rest of the question stayed on the fallback
    assert any(c.parts[0].function_response for c in pro.requests[0].contents if c.parts)  # with what flash had looked up
    assert [a["fallback"] for a in audits(capsys) if a["event"] == "model_fallback"] == ["pro"]
    assert budget.used_today() == 300  # flash's call, then both fallback calls; the failed call cost nothing

    assert ask(asker) == "second question on flash"  # the next question starts on the main model again


def test_other_model_errors_are_not_retried():
    flash = llm("flash", ValueError("bad request"))
    pro = llm("pro", "never sent")
    with pytest.raises(ValueError):
        ask(Asker(CountingStore(), flash, fallback=pro))
    assert pro.requests == []


def test_without_a_fallback_a_rate_limit_is_raised():
    with pytest.raises(RateLimited):
        ask(Asker(CountingStore(), llm("flash", RateLimited("RESOURCE_EXHAUSTED"))))
