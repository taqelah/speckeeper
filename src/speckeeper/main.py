"""The web service Cloud Run runs: Slack events and buttons, the CI webhook, a health check."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import xml.etree.ElementTree as ET

from defusedxml import DefusedXmlException
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from .audit import audit
from .config import Settings
from .service import SpecKeeper
from .slack_app import build_slack_app
from .store import FileStore, FirestoreStore, MemoryStore
from .web import WEB, landing, status_strip

logging.basicConfig(level=logging.INFO)


MAX_JUNIT_BYTES = 2_000_000
MAX_CI_BODY = 3_000_000  # the JUnit XML plus its JSON envelope


class CiRun(BaseModel):
    run_id: str
    channel: str
    junit_xml: str


def build_service(s: Settings) -> SpecKeeper:
    if s.store == "firestore":
        store = FirestoreStore(s.gcp_project, s.first_req)
    elif s.store == "file":
        store = FileStore(".data/store.json", s.first_req)
    else:
        store = MemoryStore(s.first_req)
    prd = issues = None
    if s.prd_doc_id:
        from .integrations import GoogleDocsPRD

        prd = GoogleDocsPRD(s.prd_doc_id, s.github_repo)
    if s.github_token and s.github_repo:
        from .integrations import GitHubIssues

        issues = GitHubIssues(s.github_token, s.github_repo)
    from .agent import Asker, use_vertex
    from .analytics import analytics_plugins
    from .pipelines import AdkEngine
    from .tracing import setup_tracing

    use_vertex(s.gcp_project, s.gemini_location)
    setup_tracing(s.trace_to_cloud, s.gcp_project)

    def plugins() -> list:  # a fresh set per runner: the "why?" agent, capture and thread watch
        return analytics_plugins(s.gcp_project, s.analytics_dataset, s.gcp_location)

    if s.agent_engine:  # the agent on Vertex AI Agent Engine; the local one answers if it fails, with the same Memory Bank
        from google.adk.memory import VertexAiMemoryBankService

        from .memory import LookMemory
        from .remote import RemoteAsker

        region = s.agent_engine.split("/locations/")[1].split("/")[0]
        bank = VertexAiMemoryBankService(project=s.gcp_project, location=region, agent_engine_id=s.agent_engine.rsplit("/", 1)[-1])
        local = Asker(store, s.flash_model, fallback=s.pro_model, plugins=plugins(), memory=LookMemory(bank))
        asker = RemoteAsker(s.agent_engine, fallback=local)
    else:
        asker = Asker(store, s.flash_model, fallback=s.pro_model, plugins=plugins())
    return SpecKeeper(
        store,
        AdkEngine(s.pro_model, s.flash_model, s.watch_thinking, plugins=plugins),
        prd,
        issues,
        asker,
        min_confidence=s.watch_min_confidence,
    )


def lazy_drive_fetch():
    """Drive credentials are made on first use, so the service starts before Google is set up."""
    made: list = []

    def fetch(doc_id: str):
        from .notes import drive_fetch, public_fetch

        try:
            if not made:
                made.append(drive_fetch())
            return made[0](doc_id)
        except Exception:
            logging.getLogger("speckeeper").info("Drive read failed for %s; trying the doc's public link", doc_id)
            return public_fetch(doc_id)

    return fetch


def create_app(s: Settings | None = None, svc: SpecKeeper | None = None, slack_client=None, fetch_doc=None, wait: bool = False) -> FastAPI:
    s = s or Settings.from_env()
    svc = svc or build_service(s)
    slack = build_slack_app(
        s.slack_bot_token,
        s.slack_signing_secret,
        svc,
        fetch_doc or lazy_drive_fetch(),
        client=slack_client,
        wait=wait,
        quiet=s.watch_quiet_seconds,
        approvers=s.approvers,
        per_user=s.requests_per_user,
        per_seconds=s.requests_window_seconds,
        demo_channels=s.demo_channels,
    )
    handler = AsyncSlackRequestHandler(slack)
    api = FastAPI(title="Spec Keeper", docs_url=None, redoc_url=None, openapi_url=None)  # no public API listing

    cached = {"at": 0.0, "page": ""}

    @api.get("/", response_class=HTMLResponse)
    async def home():
        # The status line reads the store, so the page is rebuilt at most every 30 seconds.
        # Build it on the first request too: on a just-started instance the monotonic clock can read under 30 s.
        if not cached["page"] or time.monotonic() - cached["at"] > 30:
            cached.update(at=time.monotonic(), page=landing(s, await asyncio.to_thread(status_strip, svc)))
        return HTMLResponse(
            cached["page"],
            headers={"Cache-Control": "public, max-age=30", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin"},
        )

    @api.get("/architecture.png")
    async def architecture():
        return FileResponse(WEB / "architecture.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @api.get("/change-sequence.png")
    async def change_sequence():
        return FileResponse(WEB / "change-sequence.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @api.get("/icon.png")
    async def icon():
        return FileResponse(WEB / "icon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @api.get("/health")
    @api.get("/healthz")  # Cloud Run's front end reserves /healthz, so on Cloud Run use /health
    async def healthz():
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        res = await handler.handle(req)
        if res.status_code == 401:  # Bolt refused the signature (or it was too old): someone is probing
            audit("slack_signature_refused", "WARNING", ip=req.client.host if req.client else "")
        return res

    @api.post("/ci")
    async def ci_webhook(req: Request):
        # The secret is checked before the body is read, so an unauthenticated caller can't make us parse
        # megabytes; bytes are compared, so an odd header is a 401, not a 500.
        given = req.headers.get("x-speckeeper-secret", "").encode("latin-1")
        if not s.ci_secret or not hmac.compare_digest(given, s.ci_secret.encode()):
            audit("ci_secret_refused", "WARNING")
            raise HTTPException(status_code=401, detail="bad secret")
        if int(req.headers.get("content-length") or 0) > MAX_CI_BODY:
            raise HTTPException(status_code=413, detail="body is over the limit")
        body = b""
        async for chunk in req.stream():
            body += chunk
            if len(body) > MAX_CI_BODY:
                raise HTTPException(status_code=413, detail="body is over the limit")
        try:
            run = CiRun.model_validate_json(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="expected JSON with run_id, channel and junit_xml") from None
        if len(run.junit_xml.encode()) > MAX_JUNIT_BYTES:
            raise HTTPException(status_code=413, detail="junit_xml is over 2 MB")
        try:
            changed, unmapped = svc.take_ci(run.junit_xml, run.run_id)
        except (ET.ParseError, DefusedXmlException):
            raise HTTPException(status_code=400, detail="junit_xml is not valid XML") from None
        allowed = await asyncio.to_thread(svc.requirement_channels)
        posted = run.channel in allowed
        if not posted and changed:
            audit("ci_channel_refused", "WARNING", channel=run.channel[:20])  # results are saved; only the Slack post is skipped
        for _req, text in changed if posted else []:
            await slack.client.chat_postMessage(channel=run.channel, text=text)
        return {"requirements": [r.key for r, _ in changed], "unmapped": unmapped, "posted": posted}

    return api


def app() -> FastAPI:  # uvicorn --factory speckeeper.main:app
    return create_app()
