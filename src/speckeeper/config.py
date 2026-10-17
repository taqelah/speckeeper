"""Settings, read once from the environment (see .env.example)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_signing_secret: str
    gcp_project: str
    gcp_location: str
    gemini_location: str  # newer Gemini models are served from the global endpoint, not every region
    pro_model: str
    flash_model: str
    prd_doc_id: str
    github_token: str
    github_repo: str
    ci_secret: str
    store: str  # "memory" for local runs and tests, "firestore" on Cloud Run
    first_req: int = 1  # the demo starts at 7, so its requirement is REQ-7 as in the video
    watch_quiet_seconds: float = 15  # thread watch looks at a thread once it has been quiet this long
    watch_min_confidence: float = 0.8  # below this, thread watch stays silent
    watch_thinking: str = "low"  # how much Flash reasons before a triage answer: low keeps suggestions quick
    approvers: tuple[str, ...] = ()  # Slack user ids who may approve or dismiss; empty: anyone in the channel
    requests_per_user: int = 50  # captures and questions per user per window: they cost Gemini quota
    requests_window_seconds: float = 600
    demo_slack_invite: str = ""  # the landing page's "Join the demo Slack" button; hidden when empty
    demo_video_url: str = ""
    contact_email: str = ""
    demo_channels: tuple[str, ...] = ()  # Slack channel ids where "@Spec Keeper reset demo" is allowed
    allowed_channels: tuple[str, ...] = ()  # channel names or ids Spec Keeper works in; empty: any channel it's in
    demo_channel_name: str = "project-checkout"  # the channel the landing page sends people to (no #)
    demo_notes_url: str = ""  # the kickoff notes link judges paste; gets a copy button on the landing page
    source_repo: str = "https://github.com/taqelah/speckeeper"
    agent_engine: str = ""  # projects/…/reasoningEngines/<id>: the "why?" agent on Agent Engine (remote.py); local when empty
    analytics_dataset: str = ""  # BigQuery dataset for the agent's analytics events; off when empty
    trace_to_cloud: bool = False  # export the question agent's spans to Cloud Trace (on in Cloud Run)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            slack_bot_token=env("SLACK_BOT_TOKEN", ""),
            slack_signing_secret=env("SLACK_SIGNING_SECRET", ""),
            gcp_project=env("GCP_PROJECT", ""),
            gcp_location=env("GCP_LOCATION", "asia-southeast1"),
            gemini_location=env("GEMINI_LOCATION", "global"),
            pro_model=env("GEMINI_PRO_MODEL", "gemini-3.1-pro-preview"),
            flash_model=env("GEMINI_FLASH_MODEL", "gemini-3.8-flash"),
            prd_doc_id=env("PRD_DOC_ID", ""),
            github_token=env("GITHUB_TOKEN", ""),
            github_repo=env("GITHUB_REPO", ""),
            ci_secret=env("CI_SECRET", ""),
            store=env("SPECKEEPER_STORE", "memory"),
            first_req=int(env("FIRST_REQ", "1")),
            watch_quiet_seconds=float(env("WATCH_QUIET_SECONDS", "15")),
            watch_min_confidence=float(env("WATCH_MIN_CONFIDENCE", "0.8")),
            watch_thinking=env("WATCH_THINKING", "low"),
            approvers=tuple(u.strip() for u in env("APPROVERS", "").split(",") if u.strip()),
            requests_per_user=int(env("REQUESTS_PER_USER", "50")),
            requests_window_seconds=float(env("REQUESTS_WINDOW_SECONDS", "600")),
            demo_slack_invite=env("DEMO_SLACK_INVITE", ""),
            demo_video_url=env("DEMO_VIDEO_URL", ""),
            contact_email=env("CONTACT_EMAIL", ""),
            demo_channel_name=env("DEMO_CHANNEL_NAME", "project-checkout").strip().lstrip("#") or "project-checkout",
            demo_notes_url=env("DEMO_NOTES_URL", ""),
            demo_channels=tuple(c.strip() for c in env("DEMO_CHANNELS", "").split(",") if c.strip()),
            allowed_channels=tuple(c.strip().lstrip("#") for c in re.split(r"[,;\s]+", env("ALLOWED_CHANNELS", "")) if c.strip()),
            source_repo=env("SOURCE_REPO", "https://github.com/taqelah/speckeeper"),
            agent_engine=env("AGENT_ENGINE", ""),
            analytics_dataset=env("ANALYTICS_DATASET", ""),
            trace_to_cloud=env("TRACE_TO_CLOUD", "") == "1",
        )
