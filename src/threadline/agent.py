"""The ADK agent that answers questions about the project's requirements ("why is the limit 10 MB?").

It is an agent, not a single schema call, because questions are open-ended: to answer "why did the
upload limit change?" it has to find the requirement, read its change history and quote the
sources, choosing the tools as it goes. Capture stays a direct schema-validated Gemini call
(extract.py), where a fixed output shape and the citation check matter more than flexibility.

Develop and debug it on its own with the ADK web UI: `uv run adk web agents`.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from .models import Requirement, Source
from .store import Store

TZ = ZoneInfo("Asia/Singapore")

INSTRUCTION = """You are Threadline, the requirements agent in a product team's Slack channel.
You answer questions about the team's requirements: what was agreed, why, when, by whom, what changed,
and whether CI has verified it.

- Use the tools to look things up. Never answer from memory or guess.
- Cite where a decision came from: the meeting (with its time) or the Slack message (who and when),
  with its link when the tool gives one, written as <link|label> for Slack.
- For "why" questions, say what changed, who approved it and when, and what the conversation said.
  If the sources don't give a reason, say so instead of inventing one.
- Be brief: two to four sentences, then the sources. Slack formatting: *bold*, no headings.
- You only read. Changing requirements happens through capture and approval, never through you."""


def _cite(s: Source) -> dict:
    return {"from": "meeting notes" if s.kind == "meeting" else "slack", "label": s.label or s.ts, "link": s.permalink, "text": s.text}


def _summary(r: Requirement) -> dict:
    return {
        "key": r.key,
        "title": r.title,
        "version": r.version,
        "status": r.status,
        "github_issue": r.issue_number,
        "verified_by_ci": r.verified,
        "acceptance_criteria": [{"key": a.key, "version": a.version, "gherkin": a.gherkin, "ci_status": a.ci_status, "last_test": a.last_test} for a in r.acs],
    }


def build_tools(store: Store) -> list:
    """The agent's tools, reading Threadline's own store. Plain functions: ADK reads their names,
    docstrings and type hints to tell Gemini what each one does."""

    def list_requirements() -> list[dict]:
        """List every approved requirement with its key, title, version and whether CI verified it."""
        return [
            {"key": r.key, "title": r.title, "version": r.version, "verified_by_ci": r.verified}
            for r in sorted(store.requirements(), key=lambda r: int(r.key.split("-")[1]))
            if r.status == "approved"
        ]

    def get_requirement(key: str) -> dict:
        """Get one requirement by key (for example REQ-7): its criteria, CI status per criterion,
        GitHub issue and the sources it was agreed in."""
        r = store.get_requirement(key.strip().upper())
        if r is None:
            return {"error": f"No requirement {key}."}
        return {**_summary(r), "sources": [_cite(s) for s in r.sources]}

    def get_history(key: str) -> list[dict]:
        """Get the decision history of a requirement (for example REQ-7): when it was first approved,
        every later change with what it said before and after, who approved it, when, and the
        conversation the change came from."""
        out = []
        for p in store.proposals_for(key.strip().upper()):
            if p.status != "approved":
                continue
            when = datetime.fromisoformat(p.decided_at).astimezone(TZ).strftime("%b %d, %H:%M") if p.decided_at else ""
            entry = {"event": "changed" if p.kind == "change" else "approved", "approved_by_slack_user": p.decided_by, "approved_at": when, "version_after": p.requirement.version, "now_says": p.requirement.title}
            if p.previous:
                entry["before"] = {"version": p.previous.version, "said": p.previous.title}
                new_ts = {s.ts for s in p.requirement.sources} - {s.ts for s in p.previous.sources}
                entry["conversation"] = [_cite(s) for s in p.requirement.sources if s.ts in new_ts]
            else:
                entry["conversation"] = [_cite(s) for s in p.requirement.sources]
            out.append(entry)
        return out or [{"error": f"No history for {key}."}]

    return [list_requirements, get_requirement, get_history]


def build_agent(store: Store, model: str) -> Agent:
    return Agent(
        name="threadline",
        description="Answers questions about the team's requirements, their history and CI status, with sources.",
        model=model,
        instruction=INSTRUCTION,
        tools=build_tools(store),
    )


def use_vertex(project: str, location: str) -> None:
    """ADK reaches Gemini through these variables: Vertex AI in our project, not an API key."""
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", location)


def rate_limited(e: Exception) -> bool:
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e)


class Asker:
    """Runs the agent for one question at a time. Each question gets a fresh session: answers come
    from the store, not from earlier chat, so two people asking get the same facts.

    Questions run on Flash (lookup and summary need no more); if a model is rate limited (429, common
    with preview models' small quotas) the question is asked once more on the fallback model."""

    def __init__(self, store: Store, model: str, fallback: str = "") -> None:
        self.runners = [InMemoryRunner(agent=build_agent(store, m), app_name="threadline") for m in dict.fromkeys([model, fallback or model])]

    async def _run(self, runner: InMemoryRunner, question: str, user: str) -> str:
        session = await runner.session_service.create_session(app_name="threadline", user_id=user, session_id=uuid.uuid4().hex)
        message = types.Content(role="user", parts=[types.Part(text=question)])
        answer = ""
        async for event in runner.run_async(user_id=user, session_id=session.id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text or "" for p in event.content.parts)
        return answer

    async def ask(self, question: str, user: str = "slack") -> str:
        for i, runner in enumerate(self.runners):
            try:
                return await self._run(runner, question, user) or "I couldn't find an answer in the requirements."
            except Exception as e:
                if not rate_limited(e) or i == len(self.runners) - 1:
                    raise
        return ""
