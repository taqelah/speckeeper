"""The "why?" agent on Agent Engine, called from Cloud Run: the request, reading the streamed answer, and
falling back to the local agent. A fake stream stands in for the engine; no network."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from speckeeper.remote import RemoteAsker, final_text

ENGINE = "projects/speckeeper-taqelah/locations/asia-southeast1/reasoningEngines/123"


def event(text, author="speckeeper", **extra):
    return "data: " + json.dumps({"author": author, "content": {"parts": [{"text": text}]}, **extra})


class Local:
    def __init__(self):
        self.questions = []

    async def ask(self, question, user="slack"):
        self.questions.append((question, user))
        return "local answer"


def test_the_final_answer_is_the_agents_last_complete_text():
    lines = [
        event("why?", author="user"),
        "data: " + json.dumps({"author": "speckeeper", "content": {"parts": [{"function_call": {"name": "get_history"}}]}}),
        event("8 MB si", partial=True),
        "data: " + json.dumps({"author": "speckeeper", "content": {"parts": [{"text": "thinking…", "thought": True}, {"text": "8 MB since Oct 15."}]}}),
        "",
        "not json",
    ]
    assert final_text(lines) == "8 MB since Oct 15."


def test_a_question_goes_to_the_engines_async_stream_query():
    sent = []

    async def post(body):
        sent.append(body)
        return [event("REQ-7 is 8 MB.")]

    asker = RemoteAsker(ENGINE, post=post)
    assert asyncio.run(asker.ask("why 8 MB?", "U_PRIYA")) == "REQ-7 is 8 MB."
    assert sent == [{"class_method": "async_stream_query", "input": {"user_id": "U_PRIYA", "message": "why 8 MB?"}}]
    assert asker.url == f"https://asia-southeast1-aiplatform.googleapis.com/v1/{ENGINE}:streamQuery?alt=sse"


@pytest.mark.parametrize("trouble", ["error", "empty", "slow"])
def test_the_local_agent_answers_when_the_engine_fails(trouble, capsys):
    async def post(body):
        if trouble == "error":
            raise RuntimeError("503")
        if trouble == "slow":
            await asyncio.sleep(1)
        return []

    local = Local()
    assert asyncio.run(RemoteAsker(ENGINE, fallback=local, timeout=0.05, post=post).ask("why?", "U_WEI")) == "local answer"
    assert local.questions == [("why?", "U_WEI")]
    assert '"agent_engine_fallback"' in capsys.readouterr().out


def test_without_a_fallback_the_failure_is_raised():
    async def post(body):
        raise RuntimeError("503")

    with pytest.raises(RuntimeError):
        asyncio.run(RemoteAsker(ENGINE, post=post).ask("why?"))


def test_agent_engine_set_means_remote_first_with_the_local_agent_and_memory_behind_it():
    from test_e2e import settings

    from speckeeper.agent import Asker
    from speckeeper.main import build_service

    assert isinstance(build_service(settings()).asker, Asker)
    asker = build_service(replace(settings(), agent_engine=ENGINE)).asker
    assert isinstance(asker, RemoteAsker) and isinstance(asker.fallback, Asker)
    agent = asker.fallback.runner.agent
    assert agent.after_tool_callback is not None
