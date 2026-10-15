"""The page at the service's root: what Spec Keeper is and how a judge can test it. Plain HTML from
web/index.html, with the links and limits filled in from settings; buttons for links not set are left out."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from .config import Settings

WEB = Path(__file__).parent / "web"


def _link(url: str, label: str, kind: str) -> str:
    return f'<a class="btn {kind}" href="{html.escape(url, quote=True)}">{html.escape(label)}</a>' if url.startswith("https://") else ""


def status_strip(svc) -> str:
    """One line of live state from the store, so judges can see the service is working: the PRD version,
    the requirements, how their criteria stand in CI, and the last approval. Never fails the page."""
    try:
        reqs = [r for r in svc.store.requirements() if r.status == "approved"]
        acs = [a for r in reqs for a in r.acs]
        count = {k: sum(1 for a in acs if a.ci_status == k) for k in ("passing", "failing", "stale", "none")}
        releases = svc.releases
    except Exception:
        return '<p class="status"><span class="live">Live on Cloud Run</span></p>'
    parts = ['<span class="live">Live on Cloud Run</span>']
    if svc.prd_version:
        parts.append(f"PRD <b>v{html.escape(svc.prd_version)}</b>")
    parts.append(f"<b>{len(reqs)}</b> requirement{'s' if len(reqs) != 1 else ''}")
    if acs:
        ci = [f"{count['passing']} passing"] + ([f"{count['failing']} failing"] if count["failing"] else [])
        ci += [f'<span class="warn">{count["stale"]} stale</span>'] if count["stale"] else []
        ci += [f"{count['none']} not tested yet"] if count["none"] else []
        parts.append("criteria in CI: " + ", ".join(ci))
    if releases:
        try:
            when = datetime.fromisoformat(releases[-1]["at"]).astimezone(ZoneInfo("Asia/Singapore")).strftime("%-d %b, %H:%M SGT")
            parts.append(f"last approval {when}")
        except (KeyError, ValueError):
            pass
    return '<p class="status" aria-label="Live status">' + " · ".join(parts) + "</p>"


def landing(s: Settings, status: str = "") -> str:
    q = lambda v: html.escape(v, quote=True)
    repo = s.source_repo or "https://github.com/taqelah/speckeeper"
    ctas = _link(s.demo_slack_invite, "Join the demo Slack", "primary") + _link(s.demo_video_url, "Watch the demo", "ghost")
    if not ctas:
        ctas = _link(repo, "See the code", "ghost")
    minutes = int(s.requests_window_seconds // 60)
    return Template((WEB / "index.html").read_text()).safe_substitute(
        ctas=ctas,
        status=status,
        notes_copy=(f'<button type="button" class="copy" data-copy="{q(s.demo_notes_url)}">Copy link</button>' if s.demo_notes_url.startswith("https://") else ""),
        limit=f"Each person can make {s.requests_per_user} requests every {minutes} minutes",
        prd=q(f"https://docs.google.com/document/d/{s.prd_doc_id}/edit" if s.prd_doc_id else repo),
        issues=q(f"https://github.com/{s.github_repo}/issues?q=label%3Arequirement" if s.github_repo else repo),
        ci=q(f"https://github.com/{s.github_repo}/actions" if s.github_repo else repo),
        repo=q(repo),
        contact=f' Questions: <a href="mailto:{q(s.contact_email)}">{html.escape(s.contact_email)}</a>' if s.contact_email else "",
    )
