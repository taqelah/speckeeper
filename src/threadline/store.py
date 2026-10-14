"""Where proposals and requirements live: in memory for local runs and tests, Firestore on Cloud Run."""

from __future__ import annotations

from typing import Protocol

from .models import Proposal, Requirement


class Store(Protocol):
    def save_proposal(self, p: Proposal) -> None: ...
    def get_proposal(self, pid: str) -> Proposal | None: ...
    def next_req_number(self) -> int: ...
    def save_requirement(self, r: Requirement) -> None: ...
    def get_requirement(self, key: str) -> Requirement | None: ...
    def requirements(self) -> list[Requirement]: ...
    def proposals_in_group(self, group_id: str) -> list[Proposal]: ...
    def get_meta(self, key: str, default: str = "") -> str: ...
    def set_meta(self, key: str, value: str) -> None: ...


class MemoryStore:
    def __init__(self, first_req: int = 1) -> None:
        self._proposals: dict[str, Proposal] = {}
        self._reqs: dict[str, Requirement] = {}
        self._next = first_req
        self._meta: dict[str, str] = {}

    def save_proposal(self, p: Proposal) -> None:
        self._proposals[p.id] = p.model_copy(deep=True)

    def get_proposal(self, pid: str) -> Proposal | None:
        p = self._proposals.get(pid)
        return p.model_copy(deep=True) if p else None

    def next_req_number(self) -> int:
        n = self._next
        self._next += 1
        return n

    def save_requirement(self, r: Requirement) -> None:
        self._reqs[r.key] = r.model_copy(deep=True)

    def get_requirement(self, key: str) -> Requirement | None:
        r = self._reqs.get(key)
        return r.model_copy(deep=True) if r else None

    def requirements(self) -> list[Requirement]:
        return [r.model_copy(deep=True) for r in self._reqs.values()]

    def proposals_in_group(self, group_id: str) -> list[Proposal]:
        return [p.model_copy(deep=True) for p in self._proposals.values() if p.group_id == group_id]

    def get_meta(self, key: str, default: str = "") -> str:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value


class FirestoreStore:
    """One document per proposal and per requirement; a counter document hands out REQ numbers."""

    def __init__(self, project: str, first_req: int = 1) -> None:
        from google.cloud import firestore

        self._fs = firestore
        self._first = first_req
        self._db = firestore.Client(project=project)

    def save_proposal(self, p: Proposal) -> None:
        self._db.collection("proposals").document(p.id).set(p.model_dump())

    def get_proposal(self, pid: str) -> Proposal | None:
        snap = self._db.collection("proposals").document(pid).get()
        return Proposal.model_validate(snap.to_dict()) if snap.exists else None

    def next_req_number(self) -> int:
        ref = self._db.collection("meta").document("counters")
        transaction = self._db.transaction()

        @self._fs.transactional
        def bump(tx) -> int:
            snap = ref.get(transaction=tx)
            n = (snap.to_dict() or {}).get("next_req", self._first) if snap.exists else self._first
            tx.set(ref, {"next_req": n + 1}, merge=True)
            return n

        return bump(transaction)

    def save_requirement(self, r: Requirement) -> None:
        self._db.collection("requirements").document(r.key).set(r.model_dump())

    def get_requirement(self, key: str) -> Requirement | None:
        snap = self._db.collection("requirements").document(key).get()
        return Requirement.model_validate(snap.to_dict()) if snap.exists else None

    def requirements(self) -> list[Requirement]:
        return [Requirement.model_validate(s.to_dict()) for s in self._db.collection("requirements").stream()]

    def proposals_in_group(self, group_id: str) -> list[Proposal]:
        docs = self._db.collection("proposals").where("group_id", "==", group_id).stream()
        return [Proposal.model_validate(d.to_dict()) for d in docs]

    def get_meta(self, key: str, default: str = "") -> str:
        snap = self._db.collection("meta").document("project").get()
        return (snap.to_dict() or {}).get(key, default) if snap.exists else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.collection("meta").document("project").set({key: value}, merge=True)
