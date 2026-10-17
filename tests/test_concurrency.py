"""Two people at once: two changes to one requirement, the same notes captured twice, two approvals
in the same instant. Threads stand in for two Cloud Run instances; the store decides who wins."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fakes import FakeEngine
from test_meeting import DOC, NOTES, FakeIssues, FakePRD, fetch, generate

from speckeeper.models import Source
from speckeeper.notes import passages
from speckeeper.service import OUTDATED, CaptureInProgress, OutdatedChange, SpecKeeper
from speckeeper.store import MemoryStore


def change_to(mb: int):
    return {
        "requirements": [
            {
                "title": f"Receipt uploads are limited to {mb} MB",
                "acceptance_criteria": [{"given": f"a {mb + 2} MB file", "when": "uploaded", "then": f"show 'File exceeds {mb} MB'"}],
                "source_ts": ["2.1"],
                "changes": "REQ-7",
            }
        ],
        "open_questions": [],
    }


def kickoff(store=None, issues=None, prd=None):
    """REQ-7 (25 MB) and REQ-8 approved; later captures answer with a change to REQ-7, 10 MB then 5 MB."""
    replies = [change_to(10), change_to(5)]

    def gen(prompt, schema):
        return replies.pop(0) if "REQ-7 |" in prompt else generate(prompt, schema)

    svc = SpecKeeper(store or MemoryStore(first_req=7), FakeEngine(gen), prd or FakePRD(), issues or FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    return svc


THREAD = [Source(ts="2.1", user="UWEI", text="Let's change the upload cap.", label="Wei, Oct 15 16:02")]


def two_change_cards(svc):
    (ten,), _ = svc.capture("C1", "2.1", THREAD)
    (five,), _ = svc.capture("C1", "3.1", THREAD)
    assert (ten.base_version, five.base_version) == (1, 1)  # both written against REQ-7 v1
    return ten, five


def test_C_28_a_change_written_against_an_older_version_is_refused_not_applied_over_the_newer_one():
    issues = FakeIssues()
    svc = kickoff(issues=issues)
    ten, five = two_change_cards(svc)
    svc.approve(ten.id, "UMAYA")
    with pytest.raises(OutdatedChange) as e:
        svc.approve(five.id, "UMAYA")
    assert (e.value.target, e.value.base, e.value.current) == ("REQ-7", 1, 2)
    r = svc.store.get_requirement("REQ-7")
    assert (r.version, r.title) == (2, "Receipt uploads are limited to 10 MB")  # the first change stands
    assert issues.updated == [(1, 2, "Receipt uploads are limited to 10 MB")]  # GitHub told once
    p = svc.store.get_proposal(five.id)
    assert (p.status, p.decided_by) == ("dismissed", OUTDATED)
    assert svc.approve(five.id, "UMAYA") == (None, svc.prd_version)  # and it can't be approved later either


def test_N_34_two_changes_to_one_requirement_approved_at_the_same_instant_file_one():
    both_read, waited = threading.Barrier(2), set()

    class SlowStore(MemoryStore):
        def get_requirement(self, key):
            r = super().get_requirement(key)
            me = threading.current_thread()
            if me is not threading.main_thread() and me not in waited:
                waited.add(me)
                both_read.wait(timeout=5)  # each approval has read REQ-7 v1 before either writes
            return r

    issues = FakeIssues()
    svc = kickoff(SlowStore(first_req=7), issues)
    ten, five = two_change_cards(svc)

    def approve(pid):
        try:
            return svc.approve(pid, "UMAYA")[0].requirement.title
        except OutdatedChange:
            return "outdated"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, [ten.id, five.id]))
    assert results.count("outdated") == 1
    (won,) = [t for t in results if t != "outdated"]
    r = svc.store.get_requirement("REQ-7")
    assert r.version == 2 and r.title == won
    assert len(issues.updated) == 1  # the loser never reached GitHub


def test_C_29_a_change_card_from_before_versions_were_recorded_still_approves():
    svc = kickoff()
    ten, _ = two_change_cards(svc)
    old = svc.store.get_proposal(ten.id)
    old.base_version = 0
    svc.store.save_proposal(old)
    assert svc.approve(ten.id, "UMAYA")[0].requirement.version == 2


def test_N_35_a_github_failure_leaves_the_requirement_as_it_was_and_the_card_approvable():
    class DownIssues(FakeIssues):
        fail = True

        def update(self, number, req):
            if self.fail:
                raise RuntimeError("GitHub 502")
            super().update(number, req)

    issues = DownIssues()
    svc = kickoff(issues=issues)
    ten, _ = two_change_cards(svc)
    with pytest.raises(RuntimeError):
        svc.approve(ten.id, "UMAYA")
    assert svc.store.get_requirement("REQ-7").version == 1
    issues.fail = False
    assert svc.approve(ten.id, "UMAYA")[0].requirement.version == 2


def test_N_36_the_same_notes_captured_twice_at_once_reach_gemini_once():
    reading, release = threading.Event(), threading.Event()
    calls = []

    def slow_fetch(doc_id):
        reading.set()
        release.wait(timeout=5)
        return fetch(doc_id)

    def gen(prompt, schema):
        calls.append(prompt)
        return generate(prompt, schema)

    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(gen), FakePRD(), FakeIssues())
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(svc.capture_notes, "C1", "1.1", DOC, slow_fetch)
        assert reading.wait(timeout=5)
        with pytest.raises(CaptureInProgress):
            svc.capture_notes("C1", "9.1", DOC, fetch)  # the second person, while the first is still reading
        release.set()
        assert first.result().status == "new"
    assert len(calls) == 1
    assert svc.capture_notes("C1", "9.1", DOC, fetch).status == "unchanged"  # once it's done: the card again, no Gemini


def test_C_30_a_capture_that_failed_frees_the_notes_and_a_crashed_one_expires():
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(generate), FakePRD(), FakeIssues())

    def broken(doc_id):
        raise PermissionError("not shared")

    with pytest.raises(PermissionError):
        svc.capture_notes("C1", "1.1", DOC, broken)
    assert svc.capture_notes("C1", "1.1", DOC, fetch).status == "new"  # the failure didn't keep the lock
    store = MemoryStore()
    assert store.claim_lock("notes-x", 0.01) and not store.claim_lock("notes-x", 60)
    time.sleep(0.02)
    assert store.claim_lock("notes-x", 60)  # nobody released it, but it ran out


def test_N_37_approvals_in_the_same_instant_get_their_own_prd_versions():
    class SlowMeta(MemoryStore):
        def set_meta(self, key, value):
            time.sleep(0.005)  # widens the gap between reading the version and writing the next one
            super().set_meta(key, value)

    prd = FakePRD()
    svc = SpecKeeper(SlowMeta(first_req=7), FakeEngine(generate), prd, FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    later = [svc.capture("C1", f"{i}.1", passages(DOC, "Sync", NOTES))[0][0] for i in range(2, 8)]
    with ThreadPoolExecutor(6) as pool:
        versions = list(pool.map(lambda p: svc.approve(p.id, "UMAYA")[1], later))
    assert sorted(versions, key=lambda v: int(v.split(".")[1])) == [f"1.{i}.0" for i in range(1, 7)]
    assert [x["version"] for x in svc.releases] == ["1.0.0"] + [f"1.{i}.0" for i in range(1, 7)]
