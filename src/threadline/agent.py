"""The ADK agent: one root agent whose tools are Threadline's actions.

Today capture is called directly (extract.py) because it needs strict structured output. This agent
is where routing, drift judging and "why?" answers grow (owner: Unmesh). Run it on its own with
`uv run adk web src` while developing.
"""

from __future__ import annotations

from google.adk.agents import Agent

from .config import Settings
from .store import MemoryStore

_store = MemoryStore()


def list_requirements() -> list[dict]:
    """Return every approved requirement with its acceptance criteria, CI status and sources."""
    return [r.model_dump() for r in _store.requirements() if r.status == "approved"]


def get_requirement(key: str) -> dict:
    """Return one requirement by key, for example REQ-7, or an empty dict if it does not exist."""
    r = _store.get_requirement(key)
    return r.model_dump() if r else {}


INSTRUCTION = """You are Threadline, a project agent in a team's Slack channel.
You keep the team's requirements, the PRD and GitHub issues true to what the team agreed.
- Answer only from the requirements and sources your tools return. Cite the source message for every claim.
- Never write to the PRD or GitHub yourself: propose, and a person approves.
- If a new message contradicts an approved requirement, name the requirement and quote both sides."""


def build_agent(s: Settings | None = None) -> Agent:
    s = s or Settings.from_env()
    return Agent(name="threadline", model=s.pro_model, instruction=INSTRUCTION, tools=[list_requirements, get_requirement])
