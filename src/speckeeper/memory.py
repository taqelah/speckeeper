"""Memory Bank for the "why?" agent: who looked at which requirement, at which version, and when.

Firestore holds the requirements; what it doesn't know is what each person last saw. When someone looks
up REQ-7, the agent records "looked at REQ-7 version 1" for them in Vertex AI Memory Bank (ADK's
VertexAiMemoryBankService, explicit memories, no Gemini call). Next time they ask, if REQ-7 has moved
on, the answer starts with what changed since they last asked: specs drift, and people notice late.

A memory is only a key, a version and a date: never a question, an answer or message text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

log = logging.getLogger("speckeeper")

APP = "speckeeper"
FACT = re.compile(r"Looked at (REQ-\d+) version (\d+) on (\d{4}-\d{2}-\d{2})")


@dataclass
class Look:
    key: str
    version: int
    on: str  # YYYY-MM-DD, UTC


class LookMemory:
    """Reads and writes looks through any ADK memory service with add_memory and search_memory."""

    def __init__(self, service) -> None:
        self.service = service

    async def last_look(self, user: str, key: str) -> Look | None:
        found = await self.service.search_memory(app_name=APP, user_id=user, query=f"Looked at {key}")
        looks = []
        for m in found.memories:
            text = "".join(p.text or "" for p in (m.content.parts or []))
            match = FACT.search(text)
            if match and match.group(1) == key:
                looks.append(Look(key, int(match.group(2)), match.group(3)))
        return max(looks, key=lambda x: (x.on, x.version), default=None)

    async def remember(self, user: str, key: str, version: int) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        fact = f"Looked at {key} version {version} on {today}"
        entry = MemoryEntry(content=types.Content(role="user", parts=[types.Part(text=fact)]))
        await self.service.add_memory(app_name=APP, user_id=user, memories=[entry])


def looked_up(tool: str, args: dict, result) -> tuple[str, int] | None:
    """(key, version now) for a successful lookup of one requirement, else None."""
    if tool == "get_requirement" and isinstance(result, dict) and "version" in result:
        return result.get("key", ""), int(result["version"])
    if tool == "get_history":
        entries = result.get("result", result) if isinstance(result, dict) else result
        versions = [e["version_after"] for e in entries or [] if isinstance(e, dict) and "version_after" in e]
        if versions:
            return str(args.get("key", "")).strip().upper(), max(versions)
    return None


async def note_changes(memory: LookMemory | None, user: str, tool: str, args: dict, result):
    """after_tool_callback work: record this look, and return the tool result with a
    changed_since_you_last_asked note if the requirement moved on since this person's last look.
    None keeps the result as it is. Memory problems never break an answer."""
    seen = looked_up(tool, args, result) if memory and user else None
    if not seen:
        return None
    key, version = seen
    try:
        before = await memory.last_look(user, key)
        await memory.remember(user, key, version)
    except Exception:
        log.warning("Memory Bank unavailable; answering without it", exc_info=True)
        return None
    if not before or before.version >= version:
        return None
    note = {"you_last_saw": f"version {before.version} on {before.on}", "now": f"version {version}"}
    return {**result, "changed_since_you_last_asked": note} if isinstance(result, dict) else {"result": result, "changed_since_you_last_asked": note}
