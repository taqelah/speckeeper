"""Settings, read once from the environment (see .env.example)."""

from __future__ import annotations

import os
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

    @classmethod
    def from_env(cls) -> "Settings":
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
        )
