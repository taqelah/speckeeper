"""Cloud Trace for the question agent: one trace per question, with a span for the agent, each Gemini
call (model, tokens) and each tool call, so a slow answer shows where its time went.

ADK already makes these OpenTelemetry spans; this only exports them to Google Cloud. It is on when
TRACE_TO_CLOUD=1 (Cloud Run), so tests and local runs export nothing."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("speckeeper")


def setup_tracing(enabled: bool) -> bool:
    if not enabled:
        return False
    # Timings, models and token counts only: ADK would otherwise copy prompts and answers into spans,
    # and Spec Keeper never stores message text outside Slack.
    os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")
    os.environ.setdefault("OTEL_SERVICE_NAME", "speckeeper")
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers

        maybe_set_otel_providers([get_gcp_exporters(enable_cloud_tracing=True)])
    except Exception:  # tracing must never stop the service from starting
        log.warning("Cloud Trace export is off: setup failed", exc_info=True)
        return False
    return True
