"""`pytest --engine=adk` runs the whole suite with capture and thread watch going through the ADK pipelines
(pipelines.py) instead of direct calls. The tests' fake Gemini functions stay the same: GenerateLlm puts
them behind ADK's model interface, so both engines are held to the same behaviour."""

from __future__ import annotations

import json

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from speckeeper.extract import DATA
from speckeeper.service import SpecKeeper


def pytest_addoption(parser):
    parser.addoption("--engine", default="direct", choices=("direct", "adk"), help="capture and thread watch engine")


class GenerateLlm(BaseLlm):
    """An ADK model that answers by calling a test's generate(prompt, schema) function."""

    source: object = None  # () -> the generate function, read at call time (tests swap it on the service)

    async def generate_content_async(self, llm_request, stream=False):
        rules = llm_request.config.system_instruction
        rules = rules if isinstance(rules, str) else "".join(p.text or "" for p in getattr(rules, "parts", []) or [])
        data = "".join(p.text or "" for c in llm_request.contents for p in c.parts or [] if c.role == "user")
        reply = self.source()(rules + DATA + data, llm_request.config.response_schema)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(reply))]))


@pytest.fixture(autouse=True)
def engine(request, monkeypatch):
    if request.config.getoption("--engine") != "adk":
        return
    from speckeeper.pipelines import AdkEngine

    original = SpecKeeper.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if self.engine is None:
            self.engine = AdkEngine(
                pro=GenerateLlm(model="test-pro", source=lambda: self.generate),
                flash=GenerateLlm(model="test-flash", source=lambda: self.triage_generate),
            )

    monkeypatch.setattr(SpecKeeper, "__init__", init)
