"""Capture: a Slack thread in, cited requirements with Gherkin acceptance criteria out.

Gemini returns structured JSON; anything that does not cite a message that is really in the thread
is dropped. A card with fewer, correct items beats one with an invented requirement.
"""

from __future__ import annotations

import logging
from typing import Callable

from pydantic import BaseModel, Field

from .models import AcceptanceCriterion, Requirement, Source


class ExtractedAC(BaseModel):
    given: str
    when: str
    then: str


class ExtractedRequirement(BaseModel):
    title: str = Field(description="One line stating the requirement, with its numbers, e.g. 'File upload size limit is 25 MB'")
    acceptance_criteria: list[ExtractedAC]
    source_ts: list[str] = Field(description="ts of every thread message this requirement was agreed in")


class Extraction(BaseModel):
    requirements: list[ExtractedRequirement]
    open_questions: list[str] = Field(default_factory=list)


PROMPT = """You turn a product team's discussion (a Slack thread, or the notes of a meeting) into requirements.

Rules:
- Only requirements the team agreed on. Proposals nobody agreed to, and anything left for later, are open questions.
- Each requirement once, even when the notes repeat it (details, decisions, summary); cite every passage it appears in.
- Each requirement gets 1 to 4 acceptance criteria in Given / When / Then form, testable as written,
  using the exact numbers from the discussion.
- source_ts lists the ts of the messages where it was stated or agreed. Never cite a ts that is not below.
- Nothing about the product that is not in the discussion.

Discussion (ts | who | text):
{thread}
"""

# (prompt, schema) -> parsed JSON as a dict. Injected so tests never call Gemini.
Generate = Callable[[str, type[BaseModel]], dict]


def thread_text(messages: list[Source]) -> str:
    return "\n".join(f"{m.ts} | {m.user or 'notes'} | {m.text}" for m in messages)


def extract(messages: list[Source], generate: Generate) -> tuple[list[Requirement], list[str]]:
    raw = Extraction.model_validate(generate(PROMPT.format(thread=thread_text(messages)), Extraction))
    by_ts = {m.ts: m for m in messages}
    reqs: list[Requirement] = []
    for item in raw.requirements:
        sources = [by_ts[ts] for ts in item.source_ts if ts in by_ts]
        if not sources or not item.acceptance_criteria:
            continue  # no citation, no card
        acs = [AcceptanceCriterion(key="", gherkin=f"Given {a.given}\nWhen {a.when}\nThen {a.then}") for a in item.acceptance_criteria]
        reqs.append(Requirement(key="", title=item.title.strip(), acs=acs, sources=sources))
    return reqs, raw.open_questions


def gemini_generate(project: str, location: str, model: str) -> Generate:
    """Gemini on Vertex AI with a response schema, so the reply is JSON of that shape."""
    from google import genai
    from google.genai import types

    clients: list = []  # made on first use, so the service starts (and Slack connects) before GCP is set up

    def generate(prompt: str, schema: type[BaseModel]) -> dict:
        if not clients:
            clients.append(genai.Client(vertexai=True, project=project, location=location))
        res = clients[0].models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.1),
        )
        u = res.usage_metadata
        if u:  # tokens are what Gemini bills for; logged per call for the cost slide and the evals
            logging.getLogger("threadline.usage").info(
                "model=%s input=%s output=%s thinking=%s", model, u.prompt_token_count, u.candidates_token_count, u.thoughts_token_count or 0
            )
            generate.last_usage = {"input": u.prompt_token_count or 0, "output": u.candidates_token_count or 0, "thinking": u.thoughts_token_count or 0}
        return res.parsed.model_dump() if isinstance(res.parsed, BaseModel) else (res.parsed or {})

    return generate
