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


def _sources_line(req: Requirement, markdown: bool = False) -> str:
    """One citation per meeting (with its times) or Slack message, never the same one twice."""
    meetings: dict[str, tuple[str, list[str]]] = {}
    others: list[str] = []
    for s in req.sources:
        if s.kind == "meeting":
            title, _, time = s.label.partition(", ")
            link, times = meetings.setdefault(title, (s.permalink, []))
            if time and time not in times:
                times.append(time)
        else:
            name = s.label or f"{s.user} {s.ts}"
            o = f"[{name}]({s.permalink})" if markdown and s.permalink else name
            if o not in others:
                others.append(o)
    parts = []
    for title, (link, times) in meetings.items():
        name = f"[{title}]({link})" if markdown and link else title
        parts.append(name + (f" ({', '.join(sorted(times))})" if times else ""))
    return "Source: " + ", ".join(parts + others)


class GoogleDocsPRD:
    """Appends one section per approved requirement. Appending (not editing in place) is deliberate:
    the Docs API edits by character index, and appends cannot land in the wrong place."""

    def __init__(self, doc_id: str) -> None:
        import google.auth
        from googleapiclient.discovery import build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/documents"])
        self.docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        self.doc_id = doc_id

    def _end(self) -> int:
        doc = self.docs.documents().get(documentId=self.doc_id).execute()
        return doc["body"]["content"][-1]["endIndex"] - 1

    def note(self, text: str) -> None:
        """A changelog line at the end of the PRD."""
        self.docs.documents().batchUpdate(documentId=self.doc_id, body={"requests": [{"insertText": {"location": {"index": self._end()}, "text": text + "\n"}}]}).execute()

    def append(self, req: Requirement) -> None:
        heading = f"{req.key} {req.title}" + (f" (v{req.version})" if req.version > 1 else "") + "\n"
        body = f"{_gherkin_block(req)}\n{_sources_line(req)}\n\n"
        end = self._end()
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
        res = self.http.post(f"/repos/{self.repo}/issues", json={"title": f"{req.key} {req.title}", "body": self._body(req), "labels": ["requirement", "ac-keyed"]})
        res.raise_for_status()
        return res.json()["number"]

    def update(self, number: int, req: Requirement) -> None:
        self.http.patch(f"/repos/{self.repo}/issues/{number}", json={"title": f"{req.key} {req.title}", "body": self._body(req)}).raise_for_status()

    @staticmethod
    def _body(req: Requirement) -> str:
        return "\n".join(
            [
                "## Acceptance criteria",
                "```gherkin",
                _gherkin_block(req),
                "```",
                "## Verification",
                *[f"- [ ] `{ac.key}@v{ac.version}` · a test whose name starts with this key" for ac in req.acs],
                "",
                f"From {req.key} in the PRD. {_sources_line(req, markdown=True)}",
                "",
                "_Opened by Threadline after approval in Slack._",
            ]
        )

    def comment(self, number: int, body: str) -> None:
        self.http.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}).raise_for_status()
