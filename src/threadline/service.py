"""What Threadline does, independent of Slack: capture, approve, dismiss, take CI results.

Slack handlers (slack_app.py) and the /ci endpoint (main.py) are thin layers over this, so the
whole loop can be tested with fakes.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from . import ci
from .extract import Generate, extract
from .models import Proposal, Requirement, Source
from .notes import FetchDoc, passages
from .store import Store

BASELINE = "1.0.0"


class PRD(Protocol):
    def append(self, req: Requirement) -> None: ...
    def note(self, text: str) -> None: ...


class Issues(Protocol):
    def create(self, req: Requirement) -> int: ...
    def comment(self, number: int, body: str) -> None: ...


class Threadline:
    def __init__(self, store: Store, generate: Generate, prd: PRD | None = None, issues: Issues | None = None) -> None:
        self.store = store
        self.generate = generate
        self.prd = prd
        self.issues = issues

    def capture(self, channel: str, thread_ts: str, messages: list[Source], group_id: str = "") -> tuple[list[Proposal], list[str]]:
        reqs, questions = extract(messages, self.generate)
        proposals = []
        for req in reqs:
            p = Proposal(id=uuid.uuid4().hex[:12], requirement=req, channel=channel, thread_ts=thread_ts, group_id=group_id)
            self.store.save_proposal(p)
            proposals.append(p)
        return proposals, questions

    def already_captured(self, doc_id: str) -> tuple[str, list[Proposal]] | None:
        """The group these notes were captured into before, if any: capturing twice must not file twice."""
        gid = self.store.get_meta(f"doc:{doc_id}")
        if not gid:
            return None
        group = [p for p in self.store.proposals_in_group(gid) if p.status != "dismissed"]
        return (gid, group) if group else None

    def capture_meeting(self, channel: str, thread_ts: str, doc_id: str, fetch: FetchDoc) -> tuple[str, str, list[Proposal], list[str]]:
        """Meeting notes in, one group of proposals out: (group id, notes title, proposals, open questions)."""
        title, text = fetch(doc_id)
        group_id = "g" + uuid.uuid4().hex[:11]
        proposals, questions = self.capture(channel, thread_ts, passages(doc_id, title, text), group_id)
        self.store.set_meta(f"doc:{doc_id}", group_id)
        return group_id, title, proposals, questions

    @property
    def prd_version(self) -> str:
        return self.store.get_meta("prd_version", "")

    def _release(self, what: str, user: str, group_id: str = "") -> str:
        """The first approval is the baseline v1.0.0, and so is everything else from the same meeting,
        however it is approved (all at once or one by one). Each later approval is a minor version."""
        current = self.prd_version
        if group_id and current and self.store.get_meta("baseline_group") == group_id:
            version = current
        elif not current:
            version = BASELINE
            self.store.set_meta("baseline_group", group_id)
        else:
            major, minor, _ = (int(x) for x in current.split("."))
            version = f"{major}.{minor + 1}.0"
        self.store.set_meta("prd_version", version)
        if self.prd:
            self.prd.note(f"v{version} · {what} · approved by {user}")
        return version

    def approve_group(self, group_id: str, user: str, user_name: str = "") -> tuple[list[Proposal], str]:
        """Approve every pending proposal from one meeting in one go: the PRD's baseline."""
        done = [p for p in (self._approve(p.id, user) for p in self.store.proposals_in_group(group_id)) if p]
        if not done:
            return [], self.prd_version
        keys = ", ".join(p.requirement.key for p in done)
        return done, self._release(f"{len(done)} requirements from the kickoff ({keys})", user_name or user, group_id)

    def set_card(self, pid: str, message_ts: str) -> None:
        p = self.store.get_proposal(pid)
        if p:
            p.message_ts = message_ts
            self.store.save_proposal(p)

    def approve(self, pid: str, user: str, user_name: str = "") -> tuple[Proposal | None, str]:
        p = self._approve(pid, user)
        if p is None:
            return None, self.prd_version
        return p, self._release(f"{p.requirement.key} {p.requirement.title}", user_name or user, p.group_id)

    def _approve(self, pid: str, user: str) -> Proposal | None:
        p = self.store.get_proposal(pid)
        if p is None or p.status != "pending":
            return None  # already decided: a double click must not file twice
        n = self.store.next_req_number()
        req = p.requirement
        req.key, req.status = f"REQ-{n}", "approved"
        for i, ac in enumerate(req.acs, 1):
            ac.key = f"AC-{n}-{i}"
        if self.prd:
            self.prd.append(req)
        if self.issues:
            req.issue_number = self.issues.create(req)
        self.store.save_requirement(req)
        p.requirement, p.status, p.decided_by = req, "approved", user
        self.store.save_proposal(p)
        return p

    def dismiss(self, pid: str, user: str) -> Proposal | None:
        p = self.store.get_proposal(pid)
        if p is None or p.status != "pending":
            return None
        p.status, p.decided_by = "dismissed", user
        p.requirement.status = "dismissed"
        self.store.save_proposal(p)
        return p

    def take_ci(self, junit_xml: str, run_id: str) -> tuple[list[tuple[Requirement, str]], list[str]]:
        """Apply a CI run. Returns (requirement, Slack summary) per requirement touched, and unmapped tests."""
        results = ci.parse_junit(junit_xml)
        changed, unmapped = ci.apply_results(self.store.requirements(), results, run_id)
        out = []
        for req in changed:
            self.store.save_requirement(req)
            text = ci.summary(req, run_id)
            if self.issues and req.issue_number:
                self.issues.comment(req.issue_number, text)
            out.append((req, text))
        return out, unmapped
