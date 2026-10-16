"""The tests' fake Gemini, behind the real ADK pipelines. A test's generate(prompt, schema) function
answers as the model; everything else (the pipelines, their check steps, the guardrails) is the code that
runs in production."""

from __future__ import annotations

import json

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from speckeeper.extract import DATA
from speckeeper.pipelines import AdkEngine


class GenerateLlm(BaseLlm):
    """An ADK model that answers by calling a test's generate(prompt, schema) with the prompt as written."""

    source: object = None  # () -> the generate function, read at call time so tests can swap it

    async def generate_content_async(self, llm_request, stream=False):
        rules = llm_request.config.system_instruction
        rules = rules if isinstance(rules, str) else "".join(p.text or "" for p in getattr(rules, "parts", []) or [])
        data = "".join(p.text or "" for c in llm_request.contents for p in c.parts or [] if c.role == "user")
        reply = self.source()(rules + DATA + data, llm_request.config.response_schema)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(reply))]))


class FakeEngine(AdkEngine):
    """AdkEngine on fake models: `generate` answers capture (the Pro agent), `triage_generate` thread watch
    (the Flash agent; the same function unless given). Tests may swap either after construction."""

    def __init__(self, generate, triage_generate=None) -> None:
        self.generate, self.triage_generate = generate, triage_generate or generate
        super().__init__(
            pro=GenerateLlm(model="test-pro", source=lambda: self.generate),
            flash=GenerateLlm(model="test-flash", source=lambda: self.triage_generate),
        )
