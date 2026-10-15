"""Memory Bank: what each person last saw, and "changed since you last asked". A fake memory service
stands in for Vertex AI Memory Bank; the agent runs for real on a scripted model."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from test_agent_callbacks import llm

from speckeeper import budget
from speckeeper.agent import Asker
from speckeeper.memory import LookMemory, looked_up, note_changes
from speckeeper.models import Requirement
from speckeeper.store import MemoryStore


class FakeMemoryBank:
    """add_memory and search_memory, like ADK's VertexAiMemoryBankService."""

    def __init__(self, broken: bool = False) -> None:
        self.facts: dict[tuple[str, str], list[str]] = {}
        self.broken = broken

    async def add_memory(self, *, app_name, user_id, memories, custom_metadata=None):
        if self.broken:
            raise RuntimeError("memory bank down")
        for m in memories:
            self.facts.setdefault((app_name, user_id), []).append("".join(p.text for p in m.content.parts))

    async def search_memory(self, *, app_name, user_id, query):
        if self.broken:
            raise RuntimeError("memory bank down")
        from google.genai import types

        return SearchMemoryResponse(memories=[MemoryEntry(content=types.Content(parts=[types.Part(text=f)])) for f in self.facts.get((app_name, user_id), [])])


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(budget, "DAILY_TOKENS", 10_000_000)
    budget._state.update(day=budget._today(), used=0, warned=False)


def run(coro):
    return asyncio.run(coro)


def test_a_look_is_remembered_as_a_key_a_version_and_a_date_only():
    bank = FakeMemoryBank()
    assert run(note_changes(LookMemory(bank), "U_PRIYA", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 1, "title": "secret"})) is None
    (fact,) = bank.facts[("speckeeper", "U_PRIYA")]
    assert fact.startswith("Looked at REQ-7 version 1 on ") and "secret" not in fact


def test_a_newer_version_since_the_last_look_is_flagged():
    memory = LookMemory(FakeMemoryBank())
    run(note_changes(memory, "U_PRIYA", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 1}))
    flagged = run(note_changes(memory, "U_PRIYA", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 2}))
    assert flagged["changed_since_you_last_asked"]["now"] == "version 2"
    assert flagged["changed_since_you_last_asked"]["you_last_saw"].startswith("version 1 on ")
    # same version again, or someone else asking: nothing to flag
    assert run(note_changes(memory, "U_PRIYA", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 2})) is None
    assert run(note_changes(memory, "U_WEI", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 2})) is None


def test_history_lookups_count_and_misses_do_not():
    assert looked_up("get_history", {"key": "req-7"}, [{"version_after": 1}, {"version_after": 2}]) == ("REQ-7", 2)
    assert looked_up("get_history", {"key": "REQ-7"}, {"result": [{"version_after": 3}]}) == ("REQ-7", 3)
    assert looked_up("get_requirement", {"key": "REQ-99"}, {"error": "No requirement REQ-99."}) is None
    assert looked_up("list_requirements", {}, [{"key": "REQ-7", "version": 2}]) is None


def test_a_memory_bank_failure_never_breaks_an_answer():
    assert run(note_changes(LookMemory(FakeMemoryBank(broken=True)), "U", "get_requirement", {"key": "REQ-7"}, {"key": "REQ-7", "version": 2})) is None


def test_the_agent_tells_someone_what_changed_since_they_last_asked():
    store = MemoryStore(first_req=7)
    store.save_requirement(Requirement(key="REQ-7", title="Receipt upload size limit is 25 MB", status="approved"))
    bank = FakeMemoryBank()
    first = llm("flash", ("get_requirement", {"key": "REQ-7"}), "25 MB.")
    asyncio.run(Asker(store, first, memory=LookMemory(bank)).ask("What is the upload limit?", "U_PRIYA"))

    store.save_requirement(Requirement(key="REQ-7", title="Receipt upload size limit is 8 MB", status="approved", version=2))
    second = llm("flash", ("get_requirement", {"key": "REQ-7"}), "Changed since you last asked: 8 MB now.")
    asyncio.run(Asker(store, second, memory=LookMemory(bank)).ask("What is the upload limit?", "U_PRIYA"))

    tool_result = json.dumps([p.function_response.response for c in second.requests[-1].contents for p in c.parts if p.function_response], default=str)
    assert "changed_since_you_last_asked" in tool_result and "version 1 on" in tool_result and "version 2" in tool_result
    assert "changed_since_you_last_asked" not in json.dumps(
        [p.function_response.response for c in first.requests[-1].contents for p in c.parts if p.function_response], default=str
    )
