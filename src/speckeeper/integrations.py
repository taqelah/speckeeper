"""The two places approved requirements are written: the PRD in Google Docs and GitHub issues."""

from __future__ import annotations

import re

import httpx

from .models import Requirement
from .prd import render, source_parts


def _gherkin_block(req: Requirement) -> str:
    out = []
    for ac in req.acs:
        steps = ac.gherkin.splitlines()
        out.append(f"{ac.key}  {steps[0]}")
        out.extend(f"{'':8}{s}" for s in steps[1:])
    return "\n".join(out)


def no_mentions(text: str) -> str:
    """GitHub text from meetings and Gemini: '@org/team' or '@user' would notify people, so a zero-width
    space after each @ keeps the text readable and the ping out."""
    return re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", text)


def _sources_line(req: Requirement, markdown: bool = False) -> str:
    """One citation per meeting (with its times) or Slack message, never the same one twice."""
    parts = [(f"[{label}]({link})" if markdown and link else label) + times for label, times, link in source_parts(req)]
    return "Source: " + ", ".join(parts)


class GoogleDocsPRD:
    """Rebuilds the whole PRD doc from the store by importing HTML into it (same doc, same link)."""

    def __init__(self, doc_id: str, repo: str = "") -> None:
        import google.auth
        from googleapiclient.discovery import build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.doc_id, self.repo = doc_id, repo

    def publish(self, reqs: list[Requirement], releases: list[dict], version: str, now: str) -> None:
        from googleapiclient.http import MediaInMemoryUpload

        name = self.drive.files().get(fileId=self.doc_id, fields="name").execute()["name"]
        title = re.sub(r"\s*[·\-|:]\s*PRD\s*$", "", name) or name  # "Checkout v2 · PRD" -> "Checkout v2"
        page = render(title, reqs, releases, version, self.repo, now)
        media = MediaInMemoryUpload(page.encode(), mimetype="text/html", resumable=False)
        self.drive.files().update(fileId=self.doc_id, media_body=media).execute()


class GitHubIssues:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self.http = client or httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20,
        )

    def create(self, req: Requirement) -> int:
        res = self.http.post(
            f"/repos/{self.repo}/issues", json={"title": no_mentions(f"{req.key} {req.title}"), "body": self._body(req), "labels": ["requirement", "ac-keyed"]}
        )
        res.raise_for_status()
        return res.json()["number"]

    def update(self, number: int, req: Requirement) -> None:
        self.http.patch(
            f"/repos/{self.repo}/issues/{number}", json={"title": no_mentions(f"{req.key} {req.title}"), "body": self._body(req)}
        ).raise_for_status()

    @staticmethod
    def _body(req: Requirement) -> str:
        return no_mentions(
            "\n".join(
                [
                    "## Acceptance criteria",
                    "```gherkin",
                    _gherkin_block(req).replace("```", "ʼʼʼ"),
                    "```",
                    "## Verification",
                    *[f"- [ ] `{ac.key}@v{ac.version}` · a test whose name starts with this key" for ac in req.acs],
                    "",
                    f"From {req.key} in the PRD. {_sources_line(req, markdown=True)}",
                    "",
                    "_Opened by Spec Keeper after approval in Slack._",
                ]
            )
        )

    def close(self, number: int, comment: str) -> None:
        self.comment(number, comment)
        self.http.patch(f"/repos/{self.repo}/issues/{number}", json={"state": "closed", "state_reason": "not_planned"}).raise_for_status()

    def comment(self, number: int, body: str) -> None:
        self.http.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": no_mentions(body)}).raise_for_status()
