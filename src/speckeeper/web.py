"""The page at the service's root: what Spec Keeper is and how a judge can test it. Plain HTML from
web/index.html, with the links and limits filled in from settings; buttons for links not set are left out."""

from __future__ import annotations

import html
from pathlib import Path
from string import Template

from .config import Settings

WEB = Path(__file__).parent / "web"


def _link(url: str, label: str, kind: str) -> str:
    return f'<a class="btn {kind}" href="{html.escape(url, quote=True)}">{html.escape(label)}</a>' if url.startswith("https://") else ""


def landing(s: Settings) -> str:
    q = lambda v: html.escape(v, quote=True)
    repo = s.source_repo or "https://github.com/taqelah/speckeeper"
    ctas = _link(s.demo_slack_invite, "Join the demo Slack", "primary") + _link(s.demo_video_url, "Watch the demo", "ghost")
    if not ctas:
        ctas = _link(repo, "See the code", "ghost")
    minutes = int(s.requests_window_seconds // 60)
    return Template((WEB / "index.html").read_text()).safe_substitute(
        ctas=ctas,
        limit=f"Each person can make {s.requests_per_user} requests every {minutes} minutes",
        prd=q(f"https://docs.google.com/document/d/{s.prd_doc_id}/edit" if s.prd_doc_id else repo),
        issues=q(f"https://github.com/{s.github_repo}/issues?q=label%3Arequirement" if s.github_repo else repo),
        ci=q(f"https://github.com/{s.github_repo}/actions" if s.github_repo else repo),
        repo=q(repo),
        contact=f' Questions: <a href="mailto:{q(s.contact_email)}">{html.escape(s.contact_email)}</a>' if s.contact_email else "",
    )
