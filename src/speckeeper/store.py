"""Where proposals and requirements live: in memory for local runs and tests, Firestore on Cloud Run."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .models import Proposal, Requirement


def _capture_order(p: Proposal) -> tuple:
    """The order a capture produced them in: the meeting's order, which decides REQ numbers."""
    return (p.created_at, p.seq)


def _history_order(p: Proposal) -> tuple:
    """Oldest first; a requirement's creation before any change made in the same instant."""
    return (p.decided_at, p.kind == "change", p.requirement.version)


class Store(Protocol):
    def save_proposal(self, p: Proposal) -> None: ...
    def get_proposal(self, pid: str) -> Proposal | None: ...
    def claim_proposal(self, pid: str) -> bool: ...
    def next_req_number(self) -> int: ...
    def save_requirement(self, r: Requirement) -> None: ...
    def replace_requirement(self, r: Requirement, expected_version: int) -> bool: ...
    def get_requirement(self, key: str) -> Requirement | None: ...
    def requirements(self) -> list[Requirement]: ...
    def proposals_in_group(self, group_id: str) -> list[Proposal]: ...
    def proposals_for(self, key: str) -> list[Proposal]: ...
    def get_meta(self, key: str, default: str = "") -> str: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def update_meta(self, change: Callable[[dict], dict]) -> dict: ...
    def claim_lock(self, name: str, seconds: float) -> bool: ...
    def release_lock(self, name: str) -> None: ...
    def clear(self) -> None: ...


_claims = threading.Lock()


class MemoryStore:
    def __init__(self, first_req: int = 1) -> None:
        self._proposals: dict[str, Proposal] = {}
        self._reqs: dict[str, Requirement] = {}
        self._next = self._first = first_req
        self._meta: dict[str, str] = {}
        self._locks: dict[str, float] = {}  # name -> when it expires (time.time())

    def clear(self) -> None:
        """Everything gone and numbering back to the start: the demo's reset."""
        self._proposals, self._reqs, self._meta, self._next, self._locks = {}, {}, {}, self._first, {}

    def save_proposal(self, p: Proposal) -> None:
        self._proposals[p.id] = p.model_copy(deep=True)

    def get_proposal(self, pid: str) -> Proposal | None:
        p = self._proposals.get(pid)
        return p.model_copy(deep=True) if p else None

    def claim_proposal(self, pid: str) -> bool:
        """pending -> deciding, for exactly one caller: two clicks (or two approvers) must not both file."""
        with _claims:
            p = self._proposals.get(pid)
            if p is None or p.status != "pending":
                return False
            p.status = "deciding"
            self.save_proposal(p)
            return True

    def next_req_number(self) -> int:
        n = self._next
        self._next += 1
        return n

    def save_requirement(self, r: Requirement) -> None:
        self._reqs[r.key] = r.model_copy(deep=True)

    def replace_requirement(self, r: Requirement, expected_version: int) -> bool:
        """Saves r only if the stored one is still approved at `expected_version`: of two changes (or a
        change and a retire) decided at once, the second finds the first's work and writes nothing."""
        with _claims:
            old = self._reqs.get(r.key)
            if old is None or old.version != expected_version or old.status != "approved":
                return False
            self.save_requirement(r)
            return True

    def get_requirement(self, key: str) -> Requirement | None:
        r = self._reqs.get(key)
        return r.model_copy(deep=True) if r else None

    def requirements(self) -> list[Requirement]:
        return [r.model_copy(deep=True) for r in self._reqs.values()]

    def proposals_in_group(self, group_id: str) -> list[Proposal]:
        return [p.model_copy(deep=True) for p in sorted(self._proposals.values(), key=_capture_order) if p.group_id == group_id]

    def proposals_for(self, key: str) -> list[Proposal]:
        """Every decided proposal that created or changed a requirement, oldest first."""
        mine = [p for p in self._proposals.values() if p.target == key or (p.kind == "new" and p.requirement.key == key)]
        return [p.model_copy(deep=True) for p in sorted(mine, key=_history_order)]

    def get_meta(self, key: str, default: str = "") -> str:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    def update_meta(self, change: Callable[[dict], dict]) -> dict:
        """change(meta) -> the keys to write, read and written as one step. Returns what was written."""
        with _claims:
            updates = change(dict(self._meta))
            for k, v in updates.items():
                self.set_meta(k, v)
            return updates

    def claim_lock(self, name: str, seconds: float) -> bool:
        """True for exactly one caller until release_lock, or until `seconds` pass (a crash mid-way)."""
        with _claims:
            now = time.time()
            if self._locks.get(name, 0) > now:
                return False
            self._locks[name] = now + seconds
            return True

    def release_lock(self, name: str) -> None:
        self._locks.pop(name, None)


class FileStore(MemoryStore):
    """MemoryStore saved to a JSON file after every write: for local runs, so a restart keeps what
    was captured and approved. Cloud Run uses Firestore."""

    def __init__(self, path: str, first_req: int = 1) -> None:
        super().__init__(first_req)
        self._path = Path(path)
        if self._path.exists():
            data = json.loads(self._path.read_text())
            self._proposals = {k: Proposal.model_validate(v) for k, v in data["proposals"].items()}
            self._reqs = {k: Requirement.model_validate(v) for k, v in data["requirements"].items()}
            self._next, self._meta = data["next"], data["meta"]

    def clear(self) -> None:
        super().clear()
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "proposals": {k: v.model_dump() for k, v in self._proposals.items()},
            "requirements": {k: v.model_dump() for k, v in self._reqs.items()},
            "next": self._next,
            "meta": self._meta,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)  # a crash mid-write never leaves half a file

    def save_proposal(self, p: Proposal) -> None:
        super().save_proposal(p)
        self._save()

    def next_req_number(self) -> int:
        n = super().next_req_number()
        self._save()
        return n

    def save_requirement(self, r: Requirement) -> None:
        super().save_requirement(r)
        self._save()

    def set_meta(self, key: str, value: str) -> None:
        super().set_meta(key, value)
        self._save()


class FirestoreStore:
    """One document per proposal and per requirement; a counter document hands out REQ numbers."""

    def __init__(self, project: str, first_req: int = 1, prefix: str = "") -> None:
        """`prefix` puts every collection under another name (\"test_proposals\"): for checks against the
        real database that must not touch production data."""
        from google.cloud import firestore

        self._fs = firestore
        self._first = first_req
        self._prefix = prefix
        self._db = firestore.Client(project=project)

    def _col(self, name: str):
        return self._db.collection(self._prefix + name)

    def clear(self) -> None:
        """Deletes every proposal, requirement, meta and lock document (the REQ counter too)."""
        for name in ("proposals", "requirements", "meta", "locks"):
            for ref in self._col(name).list_documents():
                ref.delete()

    def save_proposal(self, p: Proposal) -> None:
        self._col("proposals").document(p.id).set(p.model_dump())

    def get_proposal(self, pid: str) -> Proposal | None:
        snap = self._col("proposals").document(pid).get()
        return Proposal.model_validate(snap.to_dict()) if snap.exists else None

    def claim_proposal(self, pid: str) -> bool:
        """pending -> deciding in a transaction, so only one click or instance decides a proposal."""
        ref = self._col("proposals").document(pid)

        @self._fs.transactional
        def claim(tx) -> bool:
            snap = ref.get(transaction=tx)
            if not snap.exists or (snap.to_dict() or {}).get("status") != "pending":
                return False
            tx.update(ref, {"status": "deciding"})
            return True

        return claim(self._db.transaction())

    def next_req_number(self) -> int:
        ref = self._col("meta").document("counters")
        transaction = self._db.transaction()

        @self._fs.transactional
        def bump(tx) -> int:
            snap = ref.get(transaction=tx)
            n = (snap.to_dict() or {}).get("next_req", self._first) if snap.exists else self._first
            tx.set(ref, {"next_req": n + 1}, merge=True)
            return n

        return bump(transaction)

    def save_requirement(self, r: Requirement) -> None:
        self._col("requirements").document(r.key).set(r.model_dump())

    def replace_requirement(self, r: Requirement, expected_version: int) -> bool:
        """Saves r only if the stored one is still approved at `expected_version`, in a transaction across instances."""
        ref = self._col("requirements").document(r.key)

        @self._fs.transactional
        def swap(tx) -> bool:
            snap = ref.get(transaction=tx)
            old = (snap.to_dict() or {}) if snap.exists else {}
            if not snap.exists or old.get("version") != expected_version or old.get("status") != "approved":
                return False
            tx.set(ref, r.model_dump())
            return True

        return swap(self._db.transaction())

    def get_requirement(self, key: str) -> Requirement | None:
        snap = self._col("requirements").document(key).get()
        return Requirement.model_validate(snap.to_dict()) if snap.exists else None

    def requirements(self) -> list[Requirement]:
        return [Requirement.model_validate(s.to_dict()) for s in self._col("requirements").stream()]

    def proposals_in_group(self, group_id: str) -> list[Proposal]:
        docs = self._col("proposals").where("group_id", "==", group_id).stream()
        return sorted((Proposal.model_validate(d.to_dict()) for d in docs), key=_capture_order)

    def get_meta(self, key: str, default: str = "") -> str:
        snap = self._col("meta").document("project").get()
        return (snap.to_dict() or {}).get(key, default) if snap.exists else default

    def set_meta(self, key: str, value: str) -> None:
        self._col("meta").document("project").set({key: value}, merge=True)

    def update_meta(self, change: Callable[[dict], dict]) -> dict:
        """change(meta) -> the keys to write, in a transaction: Firestore may run `change` again on a conflict."""
        ref = self._col("meta").document("project")

        @self._fs.transactional
        def update(tx) -> dict:
            snap = ref.get(transaction=tx)
            updates = change(dict(snap.to_dict() or {}) if snap.exists else {})
            tx.set(ref, updates, merge=True)
            return updates

        return update(self._db.transaction())

    def claim_lock(self, name: str, seconds: float) -> bool:
        ref = self._col("locks").document(name)

        @self._fs.transactional
        def claim(tx) -> bool:
            snap = ref.get(transaction=tx)
            now = time.time()
            if snap.exists and (snap.to_dict() or {}).get("until", 0) > now:
                return False
            tx.set(ref, {"until": now + seconds})
            return True

        return claim(self._db.transaction())

    def release_lock(self, name: str) -> None:
        self._col("locks").document(name).delete()

    def proposals_for(self, key: str) -> list[Proposal]:
        col = self._col("proposals")
        found = {d.id: d.to_dict() for d in col.where("target", "==", key).stream()}
        found.update({d.id: d.to_dict() for d in col.where("requirement.key", "==", key).stream()})
        return sorted((Proposal.model_validate(v) for v in found.values()), key=_history_order)
