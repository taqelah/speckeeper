"""The web service Cloud Run runs: Slack events and buttons, the CI webhook, a health check."""

from __future__ import annotations

import hmac
import logging
import xml.etree.ElementTree as ET

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from .config import Settings
from .extract import Generate, gemini_generate
from .service import SpecKeeper
from .slack_app import build_slack_app
from .store import FileStore, FirestoreStore, MemoryStore

logging.basicConfig(level=logging.INFO)


class CiRun(BaseModel):
    run_id: str
    channel: str
    junit_xml: str


def build_service(s: Settings, generate: Generate | None = None) -> SpecKeeper:
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

    use_vertex(s.gcp_project, s.gemini_location)
    return SpecKeeper(
        store, generate or gemini_generate(s.gcp_project, s.gemini_location, s.pro_model, fallback=s.flash_model), prd, issues,
        Asker(store, s.flash_model, fallback=s.pro_model),
        triage_generate=generate or gemini_generate(s.gcp_project, s.gemini_location, s.flash_model, fallback=s.pro_model),
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
        s.slack_bot_token, s.slack_signing_secret, svc, fetch_doc or lazy_drive_fetch(), client=slack_client, wait=wait, quiet=s.watch_quiet_seconds
    )
    handler = AsyncSlackRequestHandler(slack)
    api = FastAPI(title="Spec Keeper", docs_url=None, redoc_url=None, openapi_url=None)  # no public API listing

    @api.get("/health")
    @api.get("/healthz")  # Cloud Run's front end reserves /healthz, so on Cloud Run use /health
    async def healthz():
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await handler.handle(req)

    @api.post("/ci")
    async def ci_webhook(run: CiRun, x_speckeeper_secret: str = Header(default="")):
        if not s.ci_secret or not hmac.compare_digest(x_speckeeper_secret, s.ci_secret):
            raise HTTPException(status_code=401, detail="bad secret")
        try:
            changed, unmapped = svc.take_ci(run.junit_xml, run.run_id)
        except ET.ParseError:
            raise HTTPException(status_code=400, detail="junit_xml is not valid XML")
        for _req, text in changed:
            await slack.client.chat_postMessage(channel=run.channel, text=text)
        return {"requirements": [r.key for r, _ in changed], "unmapped": unmapped}

    return api


def app() -> FastAPI:  # uvicorn --factory speckeeper.main:app
    return create_app()
