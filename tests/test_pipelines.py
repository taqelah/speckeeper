"""The ADK pipelines: the order of their steps, the citation check and the triage gate as agent steps, the
rules/data split, low thinking for thread watch, and the guardrails. A scripted model stands in for Gemini;
every other test also runs through these pipelines (tests/fakes.py)."""

from __future__ import annotations

import json

import pytest
from test_agent_callbacks import llm

from speckeeper import budget
from speckeeper.models import Requirement, Source
from speckeeper.pipelines import AdkEngine

MSGS = [Source(ts="1.1", user="UMAYA", text="Agreed: cap uploads at 8 MB."), Source(ts="1.2", user="UWEI", text="ok")]
REQ7 = Requirement(key="REQ-7", title="Receipt upload size limit is 25 MB", status="approved")
AC = {"given": "a 9 MB file", "when": "uploaded", "then": "it is rejected"}


def extraction(*reqs, questions=()):
    return json.dumps({"requirements": list(reqs), "open_questions": list(questions)})


def triage(verdict="change", target="REQ-7", confidence=0.9, ts=("1.1",)):
    return json.dumps({"verdict": verdict, "target": target, "confidence": confidence, "summary": "upload limit 25 MB → 8 MB", "source_ts": list(ts)})


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(budget, "DAILY_TOKENS", 10_000_000)
    budget._state.update(day=budget._today(), used=0, warned=False)


def test_capture_runs_the_model_then_the_citation_check():
    good = {"title": "Upload size limit is 8 MB", "acceptance_criteria": [AC], "source_ts": ["1.1"], "changes": "REQ-7"}
    invented = {"title": "Uploads are free", "acceptance_criteria": [AC], "source_ts": ["9.9"], "changes": ""}
    pro = llm("pro", extraction(good, invented, questions=["compression?"]))
    reqs, questions = AdkEngine(pro, llm("flash")).capture(MSGS, [REQ7])
    assert [(r.title, changes) for r, changes in reqs] == [("Upload size limit is 8 MB", "REQ-7")]
    assert [s.ts for s in reqs[0][0].sources] == ["1.1"] and questions == ["compression?"]


def test_rules_go_to_the_system_instruction_and_the_discussion_to_the_message():
    pro = llm("pro", extraction())
    AdkEngine(pro, llm("flash")).capture(MSGS, [REQ7])
    req = pro.requests[0]
    system = req.config.system_instruction if isinstance(req.config.system_instruction, str) else str(req.config.system_instruction)
    message = "".join(p.text or "" for c in req.contents for p in c.parts or [])
    assert "You turn a product team's discussion" in system and "Agreed: cap uploads" not in system
    assert "1.1 | UMAYA | Agreed: cap uploads at 8 MB." in message and "Rules:" not in message
    assert req.config.response_schema.__name__ == "Extraction" and req.config.temperature == 0.1


def test_thread_watch_suggests_through_the_gate_with_low_thinking():
    flash = llm("flash", triage())
    found = AdkEngine(llm("pro"), flash).triage([], MSGS[:1], [REQ7], 0.8)
    assert found and found.kind == "change" and found.target == "REQ-7" and [s.ts for s in found.sources] == ["1.1"]
    assert flash.requests[0].config.thinking_config.thinking_level.name == "LOW"


@pytest.mark.parametrize(
    "reply",
    [
        triage(confidence=0.5),  # not sure enough
        triage(target="REQ-99"),  # no such requirement
        triage(ts=("0.1",)),  # cites an old message, not a new one
        triage(verdict="none"),
    ],
)
def test_the_gate_stops_weak_suggestions(reply):
    assert AdkEngine(llm("pro"), llm("flash", reply)).triage([], MSGS[:1], [REQ7], 0.8) is None


def test_the_guardrails_apply_inside_the_pipelines(monkeypatch):
    monkeypatch.setattr(budget, "DAILY_TOKENS", 100)
    budget._state["used"] = 100
    pro = llm("pro", extraction())
    with pytest.raises(budget.BudgetExceeded):
        AdkEngine(pro, llm("flash")).capture(MSGS, [REQ7])
    assert pro.requests == []


def test_the_service_runs_capture_and_thread_watch_on_the_adk_pipelines():
    from test_e2e import settings

    from speckeeper.main import build_service

    engine = build_service(settings()).engine
    assert isinstance(engine, AdkEngine)
    assert engine.capture_runner.agent.name == "capture" and [a.name for a in engine.capture_runner.agent.sub_agents] == ["capture_agent", "citation_check"]
    assert engine.watch_runner.agent.name == "thread_watch" and [a.name for a in engine.watch_runner.agent.sub_agents] == ["triage_agent", "triage_gate"]


def test_the_check_steps_do_the_checking_themselves():
    # The engine re-applies the same checks when it builds requirements, so look at the pipeline's own state.
    from speckeeper.extract import capture_prompt, triage_prompt

    invented = {"title": "Uploads are free", "acceptance_criteria": [AC], "source_ts": ["9.9"], "changes": ""}
    engine = AdkEngine(llm("pro", extraction(invented)), llm("flash", triage(confidence=0.5)))
    state = engine._call(engine.capture_runner, capture_prompt(MSGS, [REQ7]), {"allowed_ts": ["1.1", "1.2"]})
    assert state["checked"]["requirements"] == [] and state["dropped"] == 1
    state = engine._call(engine.watch_runner, triage_prompt([], MSGS[:1], [REQ7]), {"new_ts": ["1.1"], "approved_keys": ["REQ-7"], "min_confidence": 0.8})
    assert state["suggest"] is False and state["triage"]["confidence"] == 0.5
