"""The page at the service's root: what Spec Keeper is and how a judge can test it. Plain HTML from
web/index.html, with the links and limits filled in from settings; buttons for links not set are left out."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from .config import Settings

WEB = Path(__file__).parent / "web"


# Slack's mark (Simple Icons, CC0), drawn in the button's text colour.
SLACK_MARK = (
    '<svg class="mark" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="currentColor"><path d="'
    "M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52z"
    "M6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313z"
    "M8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834z"
    "M8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312z"
    "M18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834z"
    "M17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312z"
    "M15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52z"
    "M15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"
    '"></path></svg>'
)


def _link(url: str, label: str, kind: str, icon: str = "") -> str:
    if not url.startswith("https://"):
        return ""
    return f'<a class="btn {kind}" href="{html.escape(url, quote=True)}">{icon}{html.escape(label)}</a>'


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
    if not reqs:
        return '<p class="status" aria-label="Live status">' + parts[0] + " · clean start: nothing approved yet</p>"
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


# Every link that leaves the page opens in a new tab, so the page stays open behind it; links within
# the page (href="#...") and links that already say where to open are left alone.
NEW_TAB = re.compile(r'<a ((?![^>]*\btarget=)[^>]*\bhref="(?!#)[^"]*"[^>]*?)>')


def command(url: str, prefix: str, otherwise: str) -> str:
    """A copyable Slack command ending in a doc link, or a plain instruction when the link isn't set."""
    if not url.startswith("https://"):
        return otherwise
    text = prefix + url
    return (
        f'<span class="cmd"><code>{html.escape(text)}</code><button type="button" class="copy" data-copy="{html.escape(text, quote=True)}">Copy</button></span>'
    )


def landing(s: Settings, status: str = "") -> str:
    def q(v):
        return html.escape(v, quote=True)

    repo = s.source_repo or "https://github.com/taqelah/speckeeper"
    ctas = _link(s.demo_slack_invite, "Join our Slack to try it", "primary", SLACK_MARK) + _link(s.demo_video_url, "Watch the demo", "ghost")
    if not ctas:
        ctas = _link(repo, "See the code", "ghost")
    minutes = int(s.requests_window_seconds // 60)
    channel = html.escape(s.demo_channel_name)
    join = ""
    if s.demo_slack_invite.startswith("https://"):
        join = (
            "<li><b>Join the demo Slack</b> (two minutes, once). Nothing to install: Slack works in the browser."
            '<ol class="sub">'
            f'<li>Open the <a href="{q(s.demo_slack_invite)}" target="_blank" rel="noopener">invitation link</a>.</li>'
            "<li>Enter your email. Slack emails you a six-character code; type it in. Not there after a minute? Check spam.</li>"
            "<li>Choose a name. If Slack offers its app, pick <b>use Slack in your browser</b> instead.</li>"
            f"<li>In the left sidebar, click <b>Channels</b> → <b>Browse channels</b> (or press Ctrl+K / ⌘K), search <b>{channel}</b>, "
            "open it and click <b>Join channel</b>. Spec Keeper is already there.</li>"
            "</ol></li>"
        )
    page = Template((WEB / "index.html").read_text()).safe_substitute(
        ctas=ctas,
        join=join,
        video_hint=f', or <a href="{q(s.demo_video_url)}" target="_blank" rel="noopener">watch the demo video</a>'
        if s.demo_video_url.startswith("https://")
        else "",
        channel=channel,
        status=status,
        notes_cmd=command(s.demo_notes_url, "@Spec Keeper ", " post <code>@Spec Keeper</code> with the pinned kickoff notes link."),
        baseline_cmd=command(s.demo_baseline_url, "@Spec Keeper ", " post <code>@Spec Keeper</code> with the pinned current product link."),
        prd_cmd=command(s.demo_prd_url, "@Spec Keeper prd Checkout v2 ", " post <code>@Spec Keeper prd Checkout v2</code> with the pinned PRD link."),
        v2prd=q(s.demo_prd_url or repo),
        limit=f"Each person can make {s.requests_per_user} requests every {minutes} minutes",
        limit_detail=(
            f"Questions and captures use Gemini, so each person gets {s.requests_per_user} every {minutes} minutes; "
            "approving, dismissing and retiring don’t count. Go over and Spec Keeper tells you, and only you, "
            "the time you can ask again. If Gemini itself is busy, it says so: try again in a minute or two. "
            "A daily AI budget shared by everyone pauses it until the next day (UTC) if it runs out. Nothing is lost either way."
        ),
        prd=q(f"https://docs.google.com/document/d/{s.prd_doc_id}/edit" if s.prd_doc_id else repo),
        issues=q(f"https://github.com/{s.github_repo}/issues?q=label%3Arequirement" if s.github_repo else repo),
        ci=q(f"https://github.com/{s.github_repo}/actions" if s.github_repo else repo),
        repo=q(repo),
        contact=f' Questions: <a href="mailto:{q(s.contact_email)}">{html.escape(s.contact_email)}</a>' if s.contact_email else "",
    )
    return NEW_TAB.sub(r'<a \1 target="_blank" rel="noopener">', page)
