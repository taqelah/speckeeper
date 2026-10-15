"""The "why?" agent as deployed to Vertex AI Agent Engine by deploy_agent.sh: the same tools, prompt and
guardrails as the local agent (speckeeper.agent), reading the same Firestore, with sessions managed by
Agent Engine and Memory Bank remembering what each person last saw (speckeeper.memory).

The engine runs in a region (ENGINE_REGION); Gemini 3.x is served from the global endpoint, so the models
get their own client location instead of the engine's GOOGLE_CLOUD_LOCATION."""

import os

from google.adk.memory import VertexAiMemoryBankService
from google.adk.models.google_llm import Gemini

from speckeeper.agent import build_agent
from speckeeper.memory import LookMemory
from speckeeper.store import FirestoreStore

PROJECT = os.environ.get("GCP_PROJECT") or os.environ["GOOGLE_CLOUD_PROJECT"]
REGION = os.environ.get("ENGINE_REGION", "asia-southeast1")
ENGINE = os.environ.get("AGENT_ENGINE_ID") or os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")


def gemini(name: str) -> Gemini:
    return Gemini(model=name, client_kwargs={"vertexai": True, "project": PROJECT, "location": os.environ.get("GEMINI_LOCATION", "global")})


memory = LookMemory(VertexAiMemoryBankService(project=PROJECT, location=REGION, agent_engine_id=ENGINE.rsplit("/", 1)[-1])) if ENGINE else None

root_agent = build_agent(
    FirestoreStore(PROJECT, int(os.environ.get("FIRST_REQ", "7"))),
    gemini(os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.8-flash")),
    fallback=gemini(os.environ.get("GEMINI_PRO_MODEL", "gemini-3.1-pro-preview")),
    memory=memory,
)
