"""BigQuery agent analytics: every "why?" question's events (each Gemini call with its tokens and latency,
each tool call with its latency) stream to BigQuery through ADK's BigQueryAgentAnalyticsPlugin, for
cost and latency queries (analytics/queries.sql).

The plugin logs prompts, answers and tool results by default. Spec Keeper never stores message text
outside Slack, so `redact` keeps only what the numbers need: event types, tool names, requirement keys,
found or not, token counts and lengths. On when ANALYTICS_DATASET is set (Cloud Run)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("speckeeper")

TABLE = "agent_events"
KEY = re.compile(r"REQ-\d{1,5}", re.IGNORECASE)


def _chars(value: Any) -> int:
    return len(value if isinstance(value, str) else json.dumps(value, default=str))


def redact(content: Any, event_type: str) -> dict | None:
    """The plugin's content_formatter: the row's content column becomes counts and names, never words."""
    c = content if isinstance(content, dict) else {}
    if event_type == "LLM_RESPONSE":
        response = str(c.get("response", ""))
        kind = response if response.startswith("call:") else f"text, {len(response)} chars"  # "call: get_history" is a tool name
        return {"response": kind, "usage": c.get("usage")}
    if event_type == "LLM_REQUEST":
        return {"messages": len(c.get("prompt") or [])}
    if event_type == "TOOL_STARTING":
        args = {k: str(v).strip() if KEY.fullmatch(str(v).strip()) else f"{_chars(v)} chars" for k, v in (c.get("args") or {}).items()}
        return {"tool": c.get("tool"), "args": args}
    if event_type == "TOOL_COMPLETED":
        result = c.get("result")
        found = not json.dumps(result, default=str).lstrip("[{").startswith('"error"')
        return {"tool": c.get("tool"), "found": found, "result_chars": _chars(result)}
    if event_type == "USER_MESSAGE_RECEIVED":
        return {"question_chars": _chars(c.get("text_summary", content))}
    if event_type == "AGENT_RESPONSE":
        return {"answer_chars": _chars(c.get("response", ""))}
    return None  # agent instructions, errors and anything new: nothing kept (fail closed)


def analytics_plugins(project: str, dataset: str, location: str = "asia-southeast1") -> list:
    """The plugin list for the agent's runner: empty unless a dataset is configured, and empty if setup fails."""
    if not dataset:
        return []
    try:
        from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin, BigQueryLoggerConfig

        config = BigQueryLoggerConfig(
            table_id=TABLE,
            content_formatter=redact,
            log_multi_modal_content=False,  # the content_parts column would repeat the question text
            flush_on_run_end=False,  # rows are written in the background, never on Slack's clock
            enable_otel_correlation=True,  # each row carries its Cloud Trace trace id
        )
        return [BigQueryAgentAnalyticsPlugin(project_id=project, dataset_id=dataset, location=location, config=config)]
    except Exception:  # analytics must never stop the service from starting
        log.warning("BigQuery analytics is off: setup failed", exc_info=True)
        return []
