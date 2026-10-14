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
    pro_model: str
    flash_model: str
    prd_doc_id: str
    github_token: str
    github_repo: str
    ci_secret: str
    store: str  # "memory" for local runs and tests, "firestore" on Cloud Run
    first_req: int = 1  # the demo starts at 7, so its requirement is REQ-7 as in the video

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            slack_bot_token=env("SLACK_BOT_TOKEN", ""),
            slack_signing_secret=env("SLACK_SIGNING_SECRET", ""),
            gcp_project=env("GCP_PROJECT", ""),
            gcp_location=env("GCP_LOCATION", "asia-southeast1"),
            pro_model=env("GEMINI_PRO_MODEL", "gemini-2.5-pro"),
            flash_model=env("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
            prd_doc_id=env("PRD_DOC_ID", ""),
            github_token=env("GITHUB_TOKEN", ""),
            github_repo=env("GITHUB_REPO", ""),
            ci_secret=env("CI_SECRET", ""),
            store=env("THREADLINE_STORE", "memory"),
            first_req=int(env("FIRST_REQ", "1")),
        )
