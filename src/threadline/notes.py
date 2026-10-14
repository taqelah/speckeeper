"""Meeting notes in: a Google Doc (Google Meet's notes, or any notes doc) read as passages.

Each non-empty line of the doc becomes a passage with its own id, so extraction can cite the exact
passage a requirement came from, and the card can show the meeting time ("00:06:45") when the
notes carry one.
"""

from __future__ import annotations

import re
from typing import Callable

from .models import Source

DOC_LINK = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]{20,})")
TIMESTAMP = re.compile(r"\((\d{1,2}:\d{2}:\d{2})\)")

# doc id -> (title, plain text). Injected so tests never call Google.
FetchDoc = Callable[[str], tuple[str, str]]


def doc_id_in(text: str) -> str | None:
    m = DOC_LINK.search(text)
    return m.group(1) if m else None


def passages(doc_id: str, title: str, text: str) -> list[Source]:
    out = []
    for i, line in enumerate(l.strip(" \t*-•") for l in text.splitlines()):
        if len(line) < 3:
            continue
        stamp = TIMESTAMP.search(line)
        label = f"{title}, {stamp.group(1)}" if stamp else title
        out.append(
            Source(
                ts=f"n{i}",
                user="",
                text=line,
                kind="meeting",
                label=label,
                permalink=f"https://docs.google.com/document/d/{doc_id}/edit",
            )
        )
    return out


def drive_fetch() -> FetchDoc:
    """Read a doc as plain text with the Drive API. Locally this runs as the service account through
    `gcloud auth application-default login --impersonate-service-account …`; on Cloud Run as the
    service's own account. Either way the notes doc must be shared with that service account."""
    import google.auth
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    def fetch(doc_id: str) -> tuple[str, str]:
        meta = drive.files().get(fileId=doc_id, fields="name", supportsAllDrives=True).execute()
        body = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
        return meta["name"], body.decode("utf-8") if isinstance(body, bytes) else body

    return fetch


def public_fetch(doc_id: str) -> tuple[str, str]:
    """A doc shared as "anyone with the link can view", read through its plain-text export. For demo
    notes only: real meeting notes stay private and go through drive_fetch."""
    import httpx

    res = httpx.get(f"https://docs.google.com/document/d/{doc_id}/export", params={"format": "txt"}, follow_redirects=True, timeout=20)
    if res.status_code != 200 or "text/plain" not in res.headers.get("content-type", ""):
        raise PermissionError("doc is not shared by link")
    text = res.content.decode("utf-8-sig")
    title = next((l.strip() for l in text.splitlines() if l.strip()), "Meeting notes")
    return title, text
