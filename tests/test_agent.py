"""The ADK agent's tools read Threadline's own store; no Gemini call here."""

from threadline.agent import build_agent, build_tools
from threadline.service import Threadline
from threadline.store import MemoryStore
from test_meeting import DOC, FakeIssues, FakePRD, fetch, generate


def test_tools_answer_from_the_store_including_change_history():
    from threadline.models import Source

    calls = []

    def gen(prompt, schema):
        calls.append(1)
        if len(calls) == 1:
            return generate(prompt, schema)
        return {"requirements": [{"title": "Receipt uploads are limited to 10 MB", "acceptance_criteria": [{"given": "a 12 MB file", "when": "uploaded", "then": "rejected"}], "source_ts": ["2.1"], "changes": "REQ-7"}], "open_questions": []}

    svc = Threadline(MemoryStore(first_req=7), gen, FakePRD(), FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    (p,), _ = svc.capture("C1", "2.1", [Source(ts="2.1", user="UWEI", text="Storage bill doubled. Cap uploads at 10 MB.", label="Wei, Oct 15 16:02", permalink="https://slack/p1")])
    svc.approve(p.id, "UMAYA")

    list_requirements, get_requirement, get_history = build_tools(svc.store)
    assert [r["key"] for r in list_requirements()] == ["REQ-7", "REQ-8"]
    assert get_requirement("req-7")["version"] == 2
    history = get_history("REQ-7")
    assert [h["event"] for h in history] == ["approved", "changed"]
    change = history[1]
    assert change["before"]["said"] == "Receipt uploads are limited to 25 MB"
    assert change["now_says"] == "Receipt uploads are limited to 10 MB"
    assert change["conversation"][0]["label"] == "Wei, Oct 15 16:02" and change["conversation"][0]["link"] == "https://slack/p1"
    assert get_requirement("REQ-99") == {"error": "No requirement REQ-99."}


def test_agent_is_built_with_the_three_tools():
    agent = build_agent(MemoryStore(), "gemini-3.1-pro-preview")
    assert agent.name == "threadline"
    assert sorted(t.__name__ for t in agent.tools) == ["get_history", "get_requirement", "list_requirements"]


def test_a_rate_limited_question_is_asked_again_on_the_fallback_model():
    import asyncio

    from threadline.agent import Asker

    asker = Asker(MemoryStore(), "gemini-3.8-flash", fallback="gemini-3.1-pro-preview")
    used = []

    class RateLimited(Exception):
        code = 429

    async def fake_run(runner, question, user):
        used.append(runner.agent.model)
        if len(used) == 1:
            raise RateLimited("RESOURCE_EXHAUSTED")
        return "answer"

    asker._run = fake_run
    assert asyncio.run(asker.ask("why?")) == "answer"
    assert used == ["gemini-3.8-flash", "gemini-3.1-pro-preview"]
