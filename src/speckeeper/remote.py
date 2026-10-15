"""The "why?" agent on Vertex AI Agent Engine, called from Cloud Run (AGENT_ENGINE set).

The agent itself is deployed with deploy_agent.sh (agents/speckeeper_engine): the same tools, prompt and
guardrails as the local one, with sessions managed by Agent Engine and Memory Bank for "changed since
you last asked". Cloud Run streams the answer from the engine's async_stream_query method. If the engine
fails or is slow, the question is answered by the local agent instead, so Slack always gets an answer."""

from __future__ import annotations

import asyncio
import json
import logging

from .audit import audit

log = logging.getLogger("speckeeper")


def final_text(lines: list[str]) -> str:
    """The agent's final answer from the engine's stream: the last event with text from the agent."""
    answer = ""
    for line in lines:
        line = line.strip().removeprefix("data:").strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        parts = ((event.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict) and not p.get("thought"))
        if text and event.get("author") != "user" and not event.get("partial"):
            answer = text
    return answer


class RemoteAsker:
    """Same interface as agent.Asker: ask(question, user) -> answer text."""

    def __init__(self, resource: str, fallback=None, timeout: float = 90, post=None) -> None:
        self.resource = resource  # projects/…/locations/<region>/reasoningEngines/<id>
        region = resource.split("/locations/")[1].split("/")[0]
        self.url = f"https://{region}-aiplatform.googleapis.com/v1/{resource}:streamQuery?alt=sse"
        self.fallback = fallback
        self.timeout = timeout
        self._post = post or self._stream

    async def _stream(self, body: dict) -> list[str]:
        import google.auth
        import google.auth.transport.requests
        import httpx

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream("POST", self.url, json=body, headers=headers) as res:
            res.raise_for_status()
            return [line async for line in res.aiter_lines()]

    async def ask(self, question: str, user: str = "slack") -> str:
        body = {"class_method": "async_stream_query", "input": {"user_id": user, "message": question}}
        try:
            answer = final_text(await asyncio.wait_for(self._post(body), self.timeout))
            if answer:
                return answer
            raise RuntimeError("the engine streamed no answer")
        except Exception as e:
            if not self.fallback:
                raise
            log.warning("Agent Engine failed (%s); answering with the local agent", type(e).__name__)
            audit("agent_engine_fallback", "WARNING", error=type(e).__name__, user=user)
            return await self.fallback.ask(question, user)
