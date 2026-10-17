"""What Spec Keeper does, independent of Slack: capture, approve, dismiss, take CI results.

Slack handlers (slack_app.py) and the /ci endpoint (main.py) are thin layers over this, so the
whole loop can be tested with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from . import ci
from .audit import audit
from .extract import Suggestion
from .models import Proposal, Requirement, Source
from .notes import FetchDoc, passages
from .store import Store

log = logging.getLogger(__name__)

BASELINE = "1.0.0"
MAX_NOTES_CHARS = 100_000  # about 40 pages; more would cost a lot and lose detail


class OutdatedChange(Exception):
    """A change card written against an older version of its requirement: approving it would undo
    whatever was approved since."""

    def __init__(self, target: str, base: int, current: int) -> None:
        super().__init__(f"{target} is at v{current}; the change was written against v{base}")
        self.target, self.base, self.current = target, base, current


class RequirementRetired(OutdatedChange):
    """A change card for a requirement that was retired since: applying it would bring it back."""

    def __init__(self, target: str, version: int) -> None:
        Exception.__init__(self, f"{target} was retired")
        self.target, self.base, self.current = target, version, version


class RetireRefused(ValueError):
    """A retire that can't be asked for; the message says why, for the person who asked. `waiting`: the
    retire already asked for, whose card the reply can link to."""

    def __init__(self, message: str, waiting: Proposal | None = None) -> None:
        super().__init__(message)
        self.waiting = waiting


class CaptureInProgress(Exception):
    """Someone else is capturing the same notes right now."""


OUTDATED = "outdated"  # decided_by of a change card that went out of date before anyone approved it
CAPTURE_LOCK_SECONDS = 600  # a capture that crashed frees its notes after this long


class NotesTooLong(ValueError):
    pass


class NotesEmpty(ValueError):
    """The doc has no line worth reading: nothing to send to Gemini, and nothing to remember."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


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
    def publish(self, reqs: list[Requirement], releases: list[dict], version: str, now: str) -> None: ...


class Issues(Protocol):
    def create(self, req: Requirement) -> int: ...
    def update(self, number: int, req: Requirement) -> None: ...
    def comment(self, number: int, body: str) -> None: ...
    def close(self, number: int, comment: str) -> None: ...


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
        acs.append(
            ac.model_copy(
                update={
                    "key": key,
                    "version": version,
                    "ci_status": status,
                    "last_test": prev.last_test if prev else "",
                    "last_run": prev.last_run if prev else "",
                }
            )
        )
    sources = old.sources + [s for s in new.sources if s not in old.sources]
    changed = old.model_copy(update={"title": new.title, "version": version, "acs": acs, "sources": sources, "status": "approved"})
    return changed, stale


class SpecKeeper:
    def __init__(
        self,
        store: Store,
        engine,
        prd: PRD | None = None,
        issues: Issues | None = None,
        asker=None,
        min_confidence: float = 0.8,
    ) -> None:
        self.store = store
        self.min_confidence = min_confidence
        self.prd = prd
        self.issues = issues
        self.asker = asker  # the ADK agent that answers questions (agent.Asker)
        self.engine = engine  # pipelines.AdkEngine: capture and thread watch as ADK multi-agent pipelines
        self._publishing = threading.Lock()  # one PRD rebuild at a time (see publish_prd)

    def capture(self, channel: str, thread_ts: str, messages: list[Source], group_id: str = "") -> tuple[list[Proposal], list[str]]:
        approved = [r for r in self.store.requirements() if r.status == "approved"]
        reqs, questions = self.engine.capture(messages, approved)
        versions = {r.key: r.version for r in approved}
        proposals = []
        now = _now()
        for seq, (req, changes) in enumerate(reqs):
            kind = "change" if changes else "new"
            p = Proposal(
                id=uuid.uuid4().hex[:12],
                kind=kind,
                target=changes,
                base_version=versions.get(changes, 0) if changes else 0,
                requirement=req,
                channel=channel,
                thread_ts=thread_ts,
                group_id=group_id,
                created_at=now,
                seq=seq,
            )
            self.store.save_proposal(p)
            proposals.append(p)
        return proposals, questions

    # --- thread watch: suggest a capture when a thread Spec Keeper was mentioned in changes a requirement ---

    @staticmethod
    def _watch_key(channel: str, thread_ts: str) -> str:
        return f"watch:{channel}:{thread_ts.replace('.', '_')}"  # no dots: Firestore reads them as field paths

    def watch_state(self, channel: str, thread_ts: str) -> dict | None:
        raw = self.store.get_meta(self._watch_key(channel, thread_ts))
        return json.loads(raw) if raw else None

    def _save_watch(self, channel: str, thread_ts: str, state: dict) -> None:
        self.store.set_meta(self._watch_key(channel, thread_ts), json.dumps(state))

    def watch(self, channel: str, thread_ts: str, since_ts: str) -> None:
        """Start watching a thread Spec Keeper was mentioned in. Messages up to `since_ts` are only context:
        what is already in the thread was the mention's business, not the watch's."""
        if self.watch_state(channel, thread_ts) is None:
            self._save_watch(channel, thread_ts, {"checked": since_ts, "muted": False, "last_suggested": 0, "suggested": []})

    def mute(self, channel: str, thread_ts: str, muted: bool, since_ts: str) -> None:
        state = self.watch_state(channel, thread_ts) or {"checked": since_ts, "last_suggested": 0, "suggested": []}
        state["muted"] = muted
        if not muted:
            state["checked"] = since_ts  # what was said while muted stays unsuggested
        self._save_watch(channel, thread_ts, state)

    def triage_thread(self, channel: str, thread_ts: str, messages: list[Source]) -> Suggestion | None:
        """Look at the messages since the last check. None (stay quiet) unless they clearly change or add a
        requirement that hasn't been suggested or proposed in this thread already."""
        state = self.watch_state(channel, thread_ts)
        if not state or state.get("muted"):
            return None
        checked = float(state["checked"])
        new = [m for m in messages if float(m.ts) > checked]
        if not new:
            return None
        earlier = [m for m in messages if float(m.ts) <= checked][-10:]
        state["checked"] = new[-1].ts  # claim them first: a retry or a second instance won't triage them again
        self._save_watch(channel, thread_ts, state)
        approved = [r for r in self.store.requirements() if r.status == "approved"]
        found = self.engine.triage(earlier, new, approved, self.min_confidence)
        if found is None:
            return None
        # The same change means the same requirement and the same numbers: 10 → 5 MB once, but 10 → 8 MB is new.
        key = f"{found.target}:{'-'.join(re.findall(r'[0-9]+(?:[.][0-9]+)?', found.summary))}" if found.target else _norm(found.summary)
        waiting = found.target and any(p.status == "pending" and p.thread_ts == thread_ts for p in self.store.proposals_for(found.target))
        if key in state["suggested"] or waiting:
            return None
        state["suggested"].append(key)
        state["last_suggested"] = time.time()
        self._save_watch(channel, thread_ts, state)
        return found

    def reset_demo(self, user: str) -> dict:
        """Back to a clean start for the next person trying the demo: requirements, proposals, versions
        and watched threads cleared, their GitHub issues closed, the PRD rebuilt empty."""
        reqs = self.store.requirements()
        closed = 0
        for r in reqs:
            if self.issues and r.issue_number:
                try:
                    self.issues.close(r.issue_number, "Closed: the Spec Keeper demo was reset in Slack.")
                    closed += 1
                except Exception:
                    log.exception("couldn't close issue %s", r.issue_number)
        self.store.clear()
        self.publish_prd()
        audit("demo_reset", user=user, requirements=len(reqs), issues_closed=closed)
        return {"requirements": len(reqs), "issues": closed}

    def not_a_change(self, channel: str, thread_ts: str, user: str, summary: str) -> None:
        """A false alarm, kept for the evals' false-alarm rate."""
        log.info("thread watch false alarm in %s/%s by %s", channel, thread_ts, user)
        self.store.set_meta("watch_false_alarms", str(int(self.store.get_meta("watch_false_alarms", "0") or 0) + 1))

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
        new decisions as new proposals, edits to approved ones as change proposals.

        One capture per doc at a time, across instances: two people pasting the same link at once
        would otherwise both reach Gemini and both get a card to approve."""
        lock = f"notes-{doc_id}"
        if not self.store.claim_lock(lock, CAPTURE_LOCK_SECONDS):
            raise CaptureInProgress(doc_id)
        try:
            return self._capture_notes(channel, thread_ts, doc_id, fetch)
        finally:
            self.store.release_lock(lock)

    def _capture_notes(self, channel: str, thread_ts: str, doc_id: str, fetch: FetchDoc) -> NotesCapture:
        title, text = fetch(doc_id)
        if len(text) > MAX_NOTES_CHARS:
            raise NotesTooLong(f"{len(text)} characters")
        lines = passages(doc_id, title, text)
        if not lines:
            raise NotesEmpty(doc_id)  # before anything is saved: filled in later, it's a first capture
        digest = fingerprint(text)
        before = self.store.get_meta(f"doc:{doc_id}")
        earlier = self.store.proposals_in_group(before) if before else []
        if earlier and all(p.status == "dismissed" for p in earlier):
            before = None  # everything was dismissed: asking again means start over
        if before and self.store.get_meta(f"dochash:{doc_id}") == digest:
            group = self.store.proposals_in_group(before)
            return NotesCapture("unchanged", before, title, group, [], self.store.get_meta(f"docat:{doc_id}"))
        if before and not earlier:
            before = None  # the last read found nothing (an empty doc, say): what's there now is the first capture
        group_id = "g" + uuid.uuid4().hex[:11]
        proposals, questions = self.capture(channel, thread_ts, lines, group_id)
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
            same = (p.kind == "new" and _norm(p.requirement.title) in titles) or (
                p.kind == "change" and target is not None and [_norm(a.gherkin) for a in target.acs] == [_norm(a.gherkin) for a in p.requirement.acs]
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
        however it is approved (all at once or one by one). Each later approval is a minor version.
        Read and written in one step, so two approvals at once get two versions and two release lines."""
        at = _now()

        def bump(meta: dict) -> dict:
            current, updates = meta.get("prd_version", ""), {}
            if group_id and current and meta.get("baseline_group") == group_id:
                version = current
            elif not current:
                version = BASELINE
                updates["baseline_group"] = group_id
            else:
                major, minor, _ = (int(x) for x in current.split("."))
                version = f"{major}.{minor + 1}.0"
            releases = json.loads(meta.get("releases", "") or "[]")
            releases.append({"version": version, "what": what, "by": user, "at": at})
            return updates | {"prd_version": version, "releases": json.dumps(releases)}

        version = self.store.update_meta(bump)["prd_version"]
        self.publish_prd()
        return version

    @property
    def releases(self) -> list[dict]:
        return json.loads(self.store.get_meta("releases", "") or "[]")

    def publish_prd(self) -> bool:
        """Rebuild the PRD doc from the store. A failure is logged, not raised: the approval stands and
        the next approval or CI run rebuilds the doc in full anyway.

        One rebuild at a time, reading the store inside the lock. Two approvals at once used to share the
        Drive client, which isn't thread-safe (the second hung for its 60 s timeout), and the slower one
        could write a list read before the other's requirement was saved."""
        if not self.prd:
            return False
        try:
            with self._publishing:
                self.prd.publish(self.store.requirements(), self.releases, self.prd_version, _now())
            return True
        except Exception:
            log.exception("Couldn't update the PRD doc")
            return False

    def approve_group(self, group_id: str, user: str, user_name: str = "") -> tuple[list[Proposal], str]:
        """Approve every pending proposal from one meeting in one go: the PRD's baseline."""
        # Numbered in the order the meeting produced them, whatever order the store returns.
        group = sorted(self.store.proposals_in_group(group_id), key=lambda p: (p.created_at, p.seq))
        done = []
        for p in group:
            try:
                if d := self._approve(p.id, user):
                    done.append(d)
            except OutdatedChange:
                continue  # marked outdated; the rest of the meeting still goes in
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
        if not self.store.claim_proposal(pid):
            return None  # already decided, or being decided: a double click must not file twice
        p = self.store.get_proposal(pid)
        try:
            done = self._approve_change(p, user) if p.kind == "change" else self._approve_new(p, user)
        except OutdatedChange:
            # No buttons come back for it: approving it later would still undo the newer version.
            p.status, p.decided_by, p.decided_at = "dismissed", OUTDATED, _now()
            p.requirement.status = "dismissed"
            self.store.save_proposal(p)
            raise
        except Exception:
            self._unclaim(pid)
            raise
        if done is None:
            self._unclaim(pid)
        return done

    def _unclaim(self, pid: str) -> None:
        p = self.store.get_proposal(pid)
        if p and p.status == "deciding":
            p.status = "pending"
            self.store.save_proposal(p)

    def _approve_new(self, p: Proposal, user: str) -> Proposal:
        n = self.store.next_req_number()
        req = p.requirement
        req.key, req.status = f"REQ-{n}", "approved"
        for i, ac in enumerate(req.acs, 1):
            ac.key = f"AC-{n}-{i}"
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
        if old.status != "approved":
            raise RequirementRetired(p.target, old.version)
        if p.base_version and old.version != p.base_version:
            raise OutdatedChange(p.target, p.base_version, old.version)  # REQ-7 moved on since this card was made
        req, stale = apply_change(old, p.requirement)
        # Saved before GitHub hears of it: of two changes approved at once, only one gets here.
        if not self.store.replace_requirement(req, old.version):
            now = self.store.get_requirement(p.target)
            if now and now.status != "approved":
                raise RequirementRetired(p.target, now.version)
            raise OutdatedChange(p.target, old.version, now.version if now else old.version)
        if self.issues and req.issue_number:
            try:
                self.issues.update(req.issue_number, req)
                note = f"Changed to v{req.version} after approval in Slack: {req.title}."
                if stale:
                    note += " Stale until their tests are updated to @v" + str(req.version) + ": " + ", ".join(stale) + "."
                self.issues.comment(req.issue_number, note)
            except Exception:
                self.store.replace_requirement(old, req.version)  # nothing half-done: the card can be approved again
                raise
        p.previous = old
        p.requirement, p.status, p.decided_by, p.decided_at = req, "approved", user, _now()
        self.store.save_proposal(p)
        return p

    def dismiss(self, pid: str, user: str) -> Proposal | None:
        if not self.store.claim_proposal(pid):
            return None
        p = self.store.get_proposal(pid)
        p.status, p.decided_by, p.decided_at = "dismissed", user, _now()
        p.requirement.status = "dismissed"
        self.store.save_proposal(p)
        return p

    # --- retire: take an approved requirement out (wrong notes, dropped scope), keeping its history ---

    def request_retire(self, keys: list[str], reason: str, channel: str, thread_ts: str, asked_by: str = "") -> list[Proposal]:
        """One card asking to retire these requirements. Nothing changes until an approver confirms it."""
        keys = list(dict.fromkeys(k.upper() for k in keys))
        if not keys:
            raise RetireRefused("Say which requirement, for example `retire REQ-12 wrong notes`.")
        reqs = []
        for key in keys:
            r = self.store.get_requirement(key)
            if r is None or r.status not in ("approved", "retired"):
                raise RetireRefused(f"{key} isn't an approved requirement.")
            if r.status == "retired":
                raise RetireRefused(f"{key} is already retired.")
            waiting = next((p for p in self.store.proposals_for(key) if p.kind == "retire" and p.status in ("pending", "deciding")), None)
            if waiting:
                raise RetireRefused(f"Retiring {key} is already waiting for an approver.", waiting)
            reqs.append(r)
        group_id, now = "r" + uuid.uuid4().hex[:11], _now()
        proposals = []
        for seq, r in enumerate(reqs):
            p = Proposal(
                id=uuid.uuid4().hex[:12],
                kind="retire",
                target=r.key,
                base_version=r.version,
                reason=reason.strip()[:300],
                asked_by=asked_by,
                requirement=r,
                channel=channel,
                thread_ts=thread_ts,
                group_id=group_id,
                created_at=now,
                seq=seq,
            )
            self.store.save_proposal(p)
            proposals.append(p)
        return proposals

    def retire_group(self, group_id: str, user: str, user_name: str = "") -> tuple[list[Proposal], str, list[str]]:
        """Retire what a card asked for. Returns (retired, PRD version, keys skipped because they changed or
        were retired since the card was posted)."""
        done, skipped = [], []
        for p in self.store.proposals_in_group(group_id):
            if p.kind != "retire" or not self.store.claim_proposal(p.id):
                continue
            r = self.store.get_requirement(p.target)
            retired = r.model_copy(update={"status": "retired"}) if r else None
            if r is None or r.version != p.base_version or not self.store.replace_requirement(retired, r.version):
                p.status, p.decided_by, p.decided_at = "dismissed", OUTDATED, _now()
                self.store.save_proposal(p)
                skipped.append(p.target)
                continue
            if self.issues and r.issue_number:
                try:
                    why = f": {p.reason}" if p.reason else ""
                    self.issues.close(r.issue_number, f"Retired after approval in Slack by {user_name or user}{why}. Spec Keeper no longer tracks it.")
                except Exception:
                    log.exception("couldn't close issue %s", r.issue_number)  # retired all the same; the issue can be closed by hand
            p.requirement, p.status, p.decided_by, p.decided_at = retired, "approved", user, _now()
            self.store.save_proposal(p)
            done.append(p)
        if not done:
            return [], self.prd_version, skipped
        keys, reason = ", ".join(p.target for p in done), done[0].reason
        version = self._release(f"{keys} retired" + (f": {reason}" if reason else ""), user_name or user)
        return done, version, skipped

    def requirement_channels(self) -> set[str]:
        """Channels where requirements were captured: the only ones a CI run may post its results in."""
        return {p.channel for r in self.store.requirements() for p in self.store.proposals_for(r.key) if p.channel}

    def take_ci(self, junit_xml: str, run_id: str) -> tuple[list[tuple[Requirement, str]], list[str]]:
        """Apply a CI run. Returns (requirement, Slack summary) per requirement touched, and unmapped tests."""
        results = ci.parse_junit(junit_xml)
        live = [r for r in self.store.requirements() if r.status == "approved"]  # a retired requirement's tests are ignored
        changed, unmapped = ci.apply_results(live, results, run_id)
        out = []
        for req in changed:
            self.store.save_requirement(req)
            text = ci.summary(req, run_id)
            if self.issues and req.issue_number:
                self.issues.comment(req.issue_number, text)
            out.append((req, text))
        if changed:
            self.publish_prd()
        return out, unmapped
