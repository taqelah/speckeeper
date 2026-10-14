"""The two places approved requirements are written: the PRD in Google Docs and GitHub issues."""

from __future__ import annotations

import httpx

from .models import Requirement


def _gherkin_block(req: Requirement) -> str:
    out = []
    for ac in req.acs:
        steps = ac.gherkin.splitlines()
        out.append(f"{ac.key}  {steps[0]}")
        out.extend(f"{'':8}{s}" for s in steps[1:])
    return "\n".join(out)


def _sources_line(req: Requirement) -> str:
    return "Source: " + ", ".join(s.permalink or f"<@{s.user}> {s.ts}" for s in req.sources)


class GoogleDocsPRD:
    """Appends one section per approved requirement. Appending (not editing in place) is deliberate:
    the Docs API edits by character index, and appends cannot land in the wrong place."""

    def __init__(self, doc_id: str) -> None:
        import google.auth
        from googleapiclient.discovery import build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/documents"])
        self.docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        self.doc_id = doc_id

    def append(self, req: Requirement) -> None:
        heading = f"{req.key} {req.title}\n"
        body = f"{_gherkin_block(req)}\n{_sources_line(req)}\n\n"
        doc = self.docs.documents().get(documentId=self.doc_id).execute()
        end = doc["body"]["content"][-1]["endIndex"] - 1
        requests = [
            {"insertText": {"location": {"index": end}, "text": heading + body}},
            {"updateParagraphStyle": {"range": {"startIndex": end, "endIndex": end + len(heading)}, "paragraphStyle": {"namedStyleType": "HEADING_2"}, "fields": "namedStyleType"}},
            {"updateTextStyle": {"range": {"startIndex": end + len(heading), "endIndex": end + len(heading) + len(_gherkin_block(req))}, "textStyle": {"weightedFontFamily": {"fontFamily": "Roboto Mono"}}, "fields": "weightedFontFamily"}},
        ]
        self.docs.documents().batchUpdate(documentId=self.doc_id, body={"requests": requests}).execute()


class GitHubIssues:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self.http = client or httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20,
        )

    def create(self, req: Requirement) -> int:
        body = "\n".join(
            [
                "## Acceptance criteria",
                "```gherkin",
                _gherkin_block(req),
                "```",
                "## Verification",
                *[f"- [ ] `{ac.key}@v{ac.version}` · a test whose name starts with this key" for ac in req.acs],
                "",
                f"From {req.key} in the PRD. {_sources_line(req)}",
                "",
                "_Opened by Threadline after approval in Slack._",
            ]
        )
        res = self.http.post(f"/repos/{self.repo}/issues", json={"title": f"{req.key} {req.title}", "body": body, "labels": ["requirement", "ac-keyed"]})
        res.raise_for_status()
        return res.json()["number"]

    def comment(self, number: int, body: str) -> None:
        self.http.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}).raise_for_status()
