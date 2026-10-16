"""Cloud Trace export: off unless asked for, never fatal, and the spans carry timings and tokens but no
message text. No network: spans go to an in-memory exporter."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_agent_callbacks import CountingStore, llm

from speckeeper import tracing
from speckeeper.agent import Asker


def test_tracing_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", raising=False)
    assert tracing.setup_tracing(False) is False
    assert "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS" not in os.environ


def test_enabling_exports_to_cloud_trace_without_message_content(monkeypatch):
    import google.adk.telemetry.google_cloud as gc
    import google.adk.telemetry.setup as setup

    monkeypatch.delenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", raising=False)
    calls = []
    monkeypatch.setattr(gc, "get_gcp_exporters", lambda **kw: calls.append(kw) or "hooks")
    monkeypatch.setattr(gc, "get_gcp_resource", lambda project: f"resource for {project}")
    monkeypatch.setattr(setup, "maybe_set_otel_providers", lambda hooks, otel_resource: calls.append((hooks, otel_resource)))
    assert tracing.setup_tracing(True, "speckeeper-taqelah") is True
    # The resource must name the project: the Telemetry API refuses spans without gcp.project_id.
    assert calls == [{"enable_cloud_tracing": True}, (["hooks"], "resource for speckeeper-taqelah")]
    assert os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"


def test_a_tracing_failure_never_stops_the_service(monkeypatch):
    import google.adk.telemetry.google_cloud as gc

    def broken(**kw):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(gc, "get_gcp_exporters", broken)
    assert tracing.setup_tracing(True) is False


@pytest.fixture
def spans(monkeypatch):
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)  # a provider for this test only
    yield exporter
    provider.shutdown()


def test_the_resource_names_the_project():
    from google.adk.telemetry.google_cloud import get_gcp_resource

    assert get_gcp_resource("speckeeper-taqelah").attributes["gcp.project_id"] == "speckeeper-taqelah"


def test_a_question_traces_the_model_and_tool_calls_but_not_the_words(spans):
    secret_question, secret_answer = "Why is the upload limit 8 MB, asks Priya?", "Because storage costs tripled."
    flash = llm("flash", ("get_requirement", {"key": "REQ-7"}), secret_answer)
    assert asyncio.run(Asker(CountingStore(), flash).ask(secret_question, "U_PRIYA")) == secret_answer

    finished = spans.get_finished_spans()
    names = [s.name for s in finished]
    assert any("call_llm" in n for n in names) and any("execute_tool" in n and "get_requirement" in n for n in names), names
    dumped = json.dumps([dict(s.attributes or {}) for s in finished], default=str)
    assert "upload limit 8 MB" not in dumped and "storage costs tripled" not in dumped
