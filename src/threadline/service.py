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
from .store import Store


class PRD(Protocol):
    def append(self, req: Requirement) -> None: ...


class Issues(Protocol):
    def create(self, req: Requirement) -> int: ...
    def comment(self, number: int, body: str) -> None: ...


class Threadline:
    def __init__(self, store: Store, generate: Generate, prd: PRD | None = None, issues: Issues | None = None) -> None:
        self.store = store
        self.generate = generate
        self.prd = prd
        self.issues = issues

    def capture(self, channel: str, thread_ts: str, messages: list[Source]) -> tuple[list[Proposal], list[str]]:
        reqs, questions = extract(messages, self.generate)
        proposals = []
        for req in reqs:
            p = Proposal(id=uuid.uuid4().hex[:12], requirement=req, channel=channel, thread_ts=thread_ts)
            self.store.save_proposal(p)
            proposals.append(p)
        return proposals, questions

    def set_card(self, pid: str, message_ts: str) -> None:
        p = self.store.get_proposal(pid)
        if p:
            p.message_ts = message_ts
            self.store.save_proposal(p)

    def approve(self, pid: str, user: str) -> Proposal | None:
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
