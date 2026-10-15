"""What Spec Keeper does, independent of Slack: capture, approve, dismiss, take CI results.

Slack handlers (slack_app.py) and the /ci endpoint (main.py) are thin layers over this, so the
whole loop can be tested with fakes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from . import ci
from .extract import Generate, extract
from .models import Proposal, Requirement, Source
from .notes import FetchDoc, passages
from .store import Store

BASELINE = "1.0.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class NotesCapture:
    """What capturing a notes doc did. `unchanged`: same text as last time, nothing was asked of Gemini.
    `changed`: the notes were edited since; proposals hold only what is new or different. `new`: first time."""

    status: Literal["new", "changed", "unchanged"]
    group_id: str
    title: str
    proposals: list[Proposal] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    captured_at: str = ""  # when these notes were last captured, for "changed since …"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def fingerprint(text: str) -> str:
    """Same notes, same fingerprint, whatever the trailing whitespace."""
    return hashlib.sha256("\n".join(line.rstrip() for line in text.strip().splitlines()).encode()).hexdigest()


class PRD(Protocol):
    def append(self, req: Requirement) -> None: ...
    def note(self, text: str) -> None: ...


class Issues(Protocol):
    def create(self, req: Requirement) -> int: ...
    def update(self, number: int, req: Requirement) -> None: ...
    def comment(self, number: int, body: str) -> None: ...


def apply_change(old: Requirement, new: Requirement) -> tuple[Requirement, list[str]]:
    """REQ-7 v1 + the agreed change -> REQ-7 v2. Keys stay; a criterion whose text changed takes the new
    version, and if a test had already proved the old text it is stale until the test is updated.
    Returns the new version and the keys of the criteria that went stale."""
    version = old.version + 1
    acs, stale = [], []
    for i, ac in enumerate(new.acs, 1):
        prev = old.acs[i - 1] if i <= len(old.acs) else None
        key = f"AC-{old.key.split('-')[1]}-{i}"
        if prev and prev.gherkin == ac.gherkin:
            acs.append(prev)  # unchanged criterion: same version, same CI status
            continue
        status = "stale" if prev and prev.last_test else "none"
        if status == "stale":
            stale.append(key)
        acs.append(ac.model_copy(update={"key": key, "version": version, "ci_status": status, "last_test": prev.last_test if prev else "", "last_run": prev.last_run if prev else ""}))
    sources = old.sources + [s for s in new.sources if s not in old.sources]
    changed = old.model_copy(update={"title": new.title, "version": version, "acs": acs, "sources": sources, "status": "approved"})
    return changed, stale


class SpecKeeper:
    def __init__(self, store: Store, generate: Generate, prd: PRD | None = None, issues: Issues | None = None, asker=None) -> None:
        self.store = store
        self.generate = generate
        self.prd = prd
        self.issues = issues
        self.asker = asker  # the ADK agent that answers questions (agent.Asker)

    def capture(self, channel: str, thread_ts: str, messages: list[Source], group_id: str = "") -> tuple[list[Proposal], list[str]]:
        approved = [r for r in self.store.requirements() if r.status == "approved"]
        reqs, questions = extract(messages, self.generate, approved)
        proposals = []
        now = _now()
        for seq, (req, changes) in enumerate(reqs):
            kind = "change" if changes else "new"
            p = Proposal(
                id=uuid.uuid4().hex[:12], kind=kind, target=changes, requirement=req, channel=channel, thread_ts=thread_ts,
                group_id=group_id, created_at=now, seq=seq,
            )
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

    def capture_notes(self, channel: str, thread_ts: str, doc_id: str, fetch: FetchDoc) -> NotesCapture:
        """Capture a notes doc, knowing what was captured from it before.

        Unchanged notes (same fingerprint) return the earlier group without calling Gemini. Edited notes
        are read again against the approved requirements, and only what is new or different comes back:
        new decisions as new proposals, edits to approved ones as change proposals."""
        title, text = fetch(doc_id)
        digest = fingerprint(text)
        before = self.store.get_meta(f"doc:{doc_id}")
        earlier = self.store.proposals_in_group(before) if before else []
        if earlier and all(p.status == "dismissed" for p in earlier):
            before = None  # everything was dismissed: asking again means start over
        if before and self.store.get_meta(f"dochash:{doc_id}") == digest:
            group = self.store.proposals_in_group(before)
            return NotesCapture("unchanged", before, title, group, [], self.store.get_meta(f"docat:{doc_id}"))
        group_id = "g" + uuid.uuid4().hex[:11]
        proposals, questions = self.capture(channel, thread_ts, passages(doc_id, title, text), group_id)
        if before:
            proposals = self._drop_repeats(proposals)
        captured_at = self.store.get_meta(f"docat:{doc_id}")
        self.store.set_meta(f"doc:{doc_id}", group_id)
        self.store.set_meta(f"dochash:{doc_id}", digest)
        self.store.set_meta(f"docat:{doc_id}", _now())
        return NotesCapture("changed" if before else "new", group_id, title, proposals, questions, captured_at)

    def _drop_repeats(self, proposals: list[Proposal]) -> list[Proposal]:
        """After an edit, Gemini should return only what's new or changed. In case it repeats an approved
        requirement anyway, drop it here: a new one with an approved title, or a change that changes nothing."""
        approved = {r.key: r for r in self.store.requirements() if r.status == "approved"}
        titles = {_norm(r.title) for r in approved.values()}
        kept = []
        for p in proposals:
            target = approved.get(p.target)
            same = (
                (p.kind == "new" and _norm(p.requirement.title) in titles)
                or (p.kind == "change" and target is not None and [_norm(a.gherkin) for a in target.acs] == [_norm(a.gherkin) for a in p.requirement.acs])
            )
            if same:
                p.status = "dismissed"
                self.store.save_proposal(p)
            else:
                kept.append(p)
        return kept

    def capture_meeting(self, channel: str, thread_ts: str, doc_id: str, fetch: FetchDoc) -> tuple[str, str, list[Proposal], list[str]]:
        """Meeting notes in, one group of proposals out: (group id, notes title, proposals, open questions)."""
        result = self.capture_notes(channel, thread_ts, doc_id, fetch)
        return result.group_id, result.title, result.proposals, result.questions

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
        # Numbered in the order the meeting produced them, whatever order the store returns.
        group = sorted(self.store.proposals_in_group(group_id), key=lambda p: (p.created_at, p.seq))
        done = [p for p in (self._approve(p.id, user) for p in group) if p]
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
        r = p.requirement
        what = f"{r.key} changed to v{r.version}: {r.title}" if p.kind == "change" else f"{r.key} {r.title}"
        return p, self._release(what, user_name or user, p.group_id)

    def _approve(self, pid: str, user: str) -> Proposal | None:
        p = self.store.get_proposal(pid)
        if p is None or p.status != "pending":
            return None  # already decided: a double click must not file twice
        if p.kind == "change":
            return self._approve_change(p, user)
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
        p.requirement, p.status, p.decided_by, p.decided_at = req, "approved", user, _now()
        self.store.save_proposal(p)
        return p

    def _approve_change(self, p: Proposal, user: str) -> Proposal | None:
        old = self.store.get_requirement(p.target)
        if old is None:
            return None
        req, stale = apply_change(old, p.requirement)
        if self.prd:
            self.prd.append(req)
        if self.issues and req.issue_number:
            self.issues.update(req.issue_number, req)
            note = f"Changed to v{req.version} after approval in Slack: {req.title}."
            if stale:
                note += " Stale until their tests are updated to @v" + str(req.version) + ": " + ", ".join(stale) + "."
            self.issues.comment(req.issue_number, note)
        self.store.save_requirement(req)
        p.previous = old
        p.requirement, p.status, p.decided_by, p.decided_at = req, "approved", user, _now()
        self.store.save_proposal(p)
        return p

    def dismiss(self, pid: str, user: str) -> Proposal | None:
        p = self.store.get_proposal(pid)
        if p is None or p.status != "pending":
            return None
        p.status, p.decided_by, p.decided_at = "dismissed", user, _now()
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
