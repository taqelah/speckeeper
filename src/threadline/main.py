"""The web service Cloud Run runs: Slack events and buttons, the CI webhook, a health check."""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from .config import Settings
from .extract import Generate, gemini_generate
from .service import Threadline
from .slack_app import build_slack_app
from .store import FileStore, FirestoreStore, MemoryStore

logging.basicConfig(level=logging.INFO)


class CiRun(BaseModel):
    run_id: str
    channel: str
    junit_xml: str


def build_service(s: Settings, generate: Generate | None = None) -> Threadline:
    if s.store == "firestore":
        store = FirestoreStore(s.gcp_project, s.first_req)
    elif s.store == "file":
        store = FileStore(".data/store.json", s.first_req)
    else:
        store = MemoryStore(s.first_req)
    prd = issues = None
    if s.prd_doc_id:
        from .integrations import GoogleDocsPRD

        prd = GoogleDocsPRD(s.prd_doc_id)
    if s.github_token and s.github_repo:
        from .integrations import GitHubIssues

        issues = GitHubIssues(s.github_token, s.github_repo)
    from .agent import Asker, use_vertex

    use_vertex(s.gcp_project, s.gemini_location)
    return Threadline(store, generate or gemini_generate(s.gcp_project, s.gemini_location, s.pro_model), prd, issues, Asker(store, s.pro_model))


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
            logging.getLogger("threadline").info("Drive read failed for %s; trying the doc's public link", doc_id)
            return public_fetch(doc_id)

    return fetch


def create_app(s: Settings | None = None, svc: Threadline | None = None) -> FastAPI:
    s = s or Settings.from_env()
    svc = svc or build_service(s)
    slack = build_slack_app(s.slack_bot_token, s.slack_signing_secret, svc, lazy_drive_fetch())
    handler = AsyncSlackRequestHandler(slack)
    api = FastAPI(title="Threadline", docs_url=None, redoc_url=None, openapi_url=None)  # no public API listing

    @api.get("/healthz")
    async def healthz():
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await handler.handle(req)

    @api.post("/ci")
    async def ci_webhook(run: CiRun, x_threadline_secret: str = Header(default="")):
        if not s.ci_secret or not hmac.compare_digest(x_threadline_secret, s.ci_secret):
            raise HTTPException(status_code=401, detail="bad secret")
        changed, unmapped = svc.take_ci(run.junit_xml, run.run_id)
        for _req, text in changed:
            await slack.client.chat_postMessage(channel=run.channel, text=text)
        return {"requirements": [r.key for r, _ in changed], "unmapped": unmapped}

    return api


def app() -> FastAPI:  # uvicorn --factory threadline.main:app
    return create_app()
