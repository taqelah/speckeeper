"""Capture and thread watch as ADK multi-agent pipelines.

    thread watch = SequentialAgent[ triage_agent (Flash, output_schema=Triage, low thinking) → triage_gate ]
    capture      = SequentialAgent[ capture_agent (Pro, output_schema=Extraction) → citation_check ]

A person's Review click stays between the two: thread watch only suggests, and capture runs when someone
asks. The prompts, schemas and checks live in extract.py; the gate and the citation check are
deterministic agent steps that call them. Both LLM agents carry the
guardrail callbacks (budget before every call, tokens counted after, fallback model on a rate limit) and
run under the same plugins as the "why?" agent, so their calls show in Cloud Trace and BigQuery too."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncGenerator, Callable

from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

from .agent import guardrails
from .extract import (
    DATA,
    PROMPT,
    TRIAGE_PROMPT,
    Extraction,
    Suggestion,
    Triage,
    capture_prompt,
    cited,
    passes_gate,
    to_requirements,
    to_suggestion,
    triage_prompt,
)
from .models import Requirement, Source


def _rules(prompt: str) -> str:
    return prompt.partition(DATA)[0]


def _llm_agent(name: str, model: str | BaseLlm, fallback: str | BaseLlm, schema: type[BaseModel], key: str, rules: str, thinking: str, label: str) -> LlmAgent:
    model_name = model if isinstance(model, str) else model.model
    return LlmAgent(
        name=name,
        model=model,
        instruction=lambda ctx: rules,  # a function, so ADK doesn't read the prompt's text as {state} placeholders
        output_schema=schema,
        output_key=key,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_level=thinking.upper()) if thinking else None,
        ),
        **guardrails(model_name, fallback, label),
    )


class CitationCheck(BaseAgent):
    """Drops every requirement that doesn't cite a message really in the discussion."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        raw = Extraction.model_validate(ctx.session.state.get("extraction") or {"requirements": []})
        kept = cited(raw, set(ctx.session.state.get("allowed_ts", [])))
        delta = {"checked": kept.model_dump(), "dropped": len(raw.requirements) - len(kept.requirements)}
        yield Event(author=self.name, invocation_id=ctx.invocation_id, actions=EventActions(state_delta=delta))


class TriageGate(BaseAgent):
    """Lets a suggestion through only if it is sure, cites the new messages and names a real requirement."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        raw = Triage.model_validate(s.get("triage") or {"verdict": "none", "confidence": 0, "summary": "", "source_ts": []})
        ok = passes_gate(raw, set(s.get("new_ts", [])), set(s.get("approved_keys", [])), float(s.get("min_confidence", 1)))
        yield Event(author=self.name, invocation_id=ctx.invocation_id, actions=EventActions(state_delta={"suggest": ok}))


class AdkEngine:
    """The ADK engine behind SpecKeeper's capture and thread watch. Sync methods, for the service's worker
    threads; the pipelines run on one background event loop, so model clients stay on one loop."""

    def __init__(self, pro: str | BaseLlm, flash: str | BaseLlm, watch_thinking: str = "low", plugins: Callable[[], list] = lambda: []) -> None:
        capture = SequentialAgent(
            name="capture",
            sub_agents=[
                _llm_agent("capture_agent", pro, flash, Extraction, "extraction", _rules(PROMPT), "", "capture"),
                CitationCheck(name="citation_check"),
            ],
        )
        watch = SequentialAgent(
            name="thread_watch",
            sub_agents=[
                _llm_agent("triage_agent", flash, pro, Triage, "triage", _rules(TRIAGE_PROMPT), watch_thinking, "watch"),
                TriageGate(name="triage_gate"),
            ],
        )
        self.capture_runner = InMemoryRunner(agent=capture, app_name="speckeeper_capture", plugins=plugins())
        self.watch_runner = InMemoryRunner(agent=watch, app_name="speckeeper_watch", plugins=plugins())
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="adk-pipelines", daemon=True).start()

    async def _run(self, runner: InMemoryRunner, prompt: str, state: dict) -> dict:
        app, user = runner.app_name, "speckeeper"
        session = await runner.session_service.create_session(app_name=app, user_id=user, session_id=uuid.uuid4().hex, state=state)
        message = types.Content(role="user", parts=[types.Part(text=prompt.partition(DATA)[2])])  # the data; the rules are the instruction
        try:
            async for _ in runner.run_async(user_id=user, session_id=session.id, new_message=message):
                pass
            done = await runner.session_service.get_session(app_name=app, user_id=user, session_id=session.id)
            return dict(done.state)
        finally:
            await runner.session_service.delete_session(app_name=app, user_id=user, session_id=session.id)  # one session per call

    def _call(self, runner: InMemoryRunner, prompt: str, state: dict) -> dict:
        return asyncio.run_coroutine_threadsafe(self._run(runner, prompt, state), self.loop).result()

    def capture(self, messages: list[Source], approved: list[Requirement]) -> tuple[list[tuple[Requirement, str]], list[str]]:
        state = self._call(self.capture_runner, capture_prompt(messages, approved), {"allowed_ts": [m.ts for m in messages]})
        return to_requirements(Extraction.model_validate(state.get("checked") or {"requirements": []}), messages, approved)

    def triage(self, context: list[Source], new: list[Source], approved: list[Requirement], min_confidence: float) -> Suggestion | None:
        if not new:
            return None
        state = {"new_ts": [m.ts for m in new], "approved_keys": [r.key for r in approved], "min_confidence": min_confidence}
        done = self._call(self.watch_runner, triage_prompt(context, new, approved), state)
        raw = Triage.model_validate(done.get("triage") or {"verdict": "none", "confidence": 0, "summary": "", "source_ts": []})
        suggestion = to_suggestion(raw, new, approved, min_confidence)  # logs the verdict and builds the card's sources
        return suggestion if done.get("suggest") else None
