"""Retire: an approved requirement taken out (wrong notes, dropped scope). Asked by anyone, confirmed by an
approver; it leaves the PRD and CI, its issue closes, and its history stays."""

import pytest
from fakes import FakeEngine
from test_concurrency import THREAD, kickoff
from test_meeting import DOC, FakeIssues, FakePRD, fetch, generate

from speckeeper.agent import build_tools
from speckeeper.service import OUTDATED, RequirementRetired, RetireRefused, SpecKeeper
from speckeeper.store import MemoryStore


class Issues(FakeIssues):
    def close(self, number, comment):
        super().close(number, comment)
        self.comments.append((number, comment))


def approved(issues=None, prd=None):
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(generate), prd or FakePRD(), issues or Issues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    return svc


def test_R_01_asking_changes_nothing_until_an_approver_confirms():
    issues, prd = Issues(), FakePRD()
    svc = approved(issues, prd)
    (p,) = svc.request_retire(["req-7"], "wrong notes", "C1", "5.1", "UWEI")
    assert (p.kind, p.target, p.base_version, p.reason, p.asked_by) == ("retire", "REQ-7", 1, "wrong notes", "UWEI")
    assert svc.store.get_requirement("REQ-7").status == "approved" and not getattr(issues, "closed", [])
    assert prd.appended == ["REQ-7", "REQ-8"] and svc.prd_version == "1.0.0"


def test_R_02_retired_leaves_the_prd_closes_the_issue_and_keeps_its_history():
    issues, prd = Issues(), FakePRD()
    svc = approved(issues, prd)
    (p,) = svc.request_retire(["REQ-7"], "wrong notes", "C1", "5.1", "UWEI")
    done, version, skipped = svc.retire_group(p.group_id, "UMAYA", "maya")
    assert [d.target for d in done] == ["REQ-7"] and skipped == [] and version == "1.1.0"
    r = svc.store.get_requirement("REQ-7")
    assert r.status == "retired" and r.version == 1 and r.acs  # still there, with its criteria
    assert prd.appended == ["REQ-8"] and prd.notes[-1] == "v1.1.0 · REQ-7 retired: wrong notes · approved by maya"
    assert issues.closed == [1] and "Retired after approval in Slack by maya: wrong notes" in issues.comments[-1][1]

    tools = {t.__name__: t for t in build_tools(svc.store)}
    assert [x["key"] for x in tools["list_requirements"]()] == ["REQ-8"]
    assert tools["get_requirement"]("REQ-7")["status"] == "retired"
    history = tools["get_history"]("REQ-7")
    assert history[0]["event"] == "approved" and history[-1] == {**history[-1], "event": "retired", "reason": "wrong notes"}


def test_R_03_one_card_retires_a_whole_wrong_meeting_in_one_release():
    prd = FakePRD()
    svc = approved(prd=prd)
    ps = svc.request_retire(["REQ-7", "REQ-8", "REQ-7"], "", "C1", "5.1")  # a repeated key counts once
    assert len(ps) == 2 and len({p.group_id for p in ps}) == 1
    done, version, _ = svc.retire_group(ps[0].group_id, "UMAYA")
    assert len(done) == 2 and version == "1.1.0" and prd.appended == []
    assert prd.notes[-1].startswith("v1.1.0 · REQ-7, REQ-8 retired ·")  # no reason: no ": …"


@pytest.mark.parametrize(
    ("keys", "says"),
    [([], "Say which requirement"), (["REQ-99"], "REQ-99 isn't an approved requirement"), (["REQ-8", "REQ-99"], "REQ-99 isn't")],
)
def test_N_41_unknown_or_missing_keys_are_refused_and_nothing_is_asked(keys, says):
    svc = approved()
    with pytest.raises(RetireRefused, match=says):
        svc.request_retire(keys, "", "C1", "5.1")
    assert not any(p.kind == "retire" for p in svc.store.proposals_for("REQ-8"))  # all or nothing


def test_N_42_already_retired_or_already_asked_is_refused():
    svc = approved()
    (p,) = svc.request_retire(["REQ-7"], "", "C1", "5.1")
    with pytest.raises(RetireRefused, match="already waiting"):
        svc.request_retire(["REQ-7"], "", "C1", "6.1")
    svc.retire_group(p.group_id, "UMAYA")
    with pytest.raises(RetireRefused, match="already retired"):
        svc.request_retire(["REQ-7"], "", "C1", "6.1")


def test_N_43_two_confirm_clicks_retire_once():
    issues, prd = Issues(), FakePRD()
    svc = approved(issues, prd)
    (p,) = svc.request_retire(["REQ-7"], "", "C1", "5.1")
    assert len(svc.retire_group(p.group_id, "UMAYA")[0]) == 1
    assert svc.retire_group(p.group_id, "UMAYA") == ([], "1.1.0", [])
    assert issues.closed == [1] and len(prd.notes) == 2


def test_C_37_a_requirement_changed_after_the_ask_is_not_retired():
    svc = kickoff()
    (p,) = svc.request_retire(["REQ-7"], "wrong notes", "C1", "5.1")
    (ten,), _ = svc.capture("C1", "2.1", THREAD)
    svc.approve(ten.id, "UMAYA")  # REQ-7 is v2 now: the card asked about v1
    done, _, skipped = svc.retire_group(p.group_id, "UMAYA")
    assert done == [] and skipped == ["REQ-7"]
    assert svc.store.get_requirement("REQ-7").status == "approved"
    assert svc.store.get_proposal(p.id).decided_by == OUTDATED


def test_C_38_a_change_card_for_a_retired_requirement_cannot_bring_it_back():
    svc = kickoff()
    (ten,), _ = svc.capture("C1", "2.1", THREAD)
    (p,) = svc.request_retire(["REQ-7"], "", "C1", "5.1")
    svc.retire_group(p.group_id, "UMAYA")
    with pytest.raises(RequirementRetired):
        svc.approve(ten.id, "UMAYA")
    r = svc.store.get_requirement("REQ-7")
    assert (r.status, r.version) == ("retired", 1)


def test_C_39_ci_ignores_a_retired_requirement_s_tests():
    issues = Issues()
    svc = approved(issues)
    (p,) = svc.request_retire(["REQ-7"], "", "C1", "5.1")
    svc.retire_group(p.group_id, "UMAYA")
    before = len(issues.comments)
    junit = '<testsuites><testsuite><testcase name="AC-7-1@v1 rejects a 30 MB file"/></testsuite></testsuites>'
    out, _ = svc.take_ci(junit, "#9")
    assert out == [] and len(issues.comments) == before
    assert svc.store.get_requirement("REQ-7").acs[0].ci_status == "none"


def test_C_40_a_github_failure_still_retires_and_the_issue_can_be_closed_by_hand():
    class Down(Issues):
        def close(self, number, comment):
            raise RuntimeError("GitHub 502")

    svc = approved(Down())
    (p,) = svc.request_retire(["REQ-7"], "", "C1", "5.1")
    done, version, _ = svc.retire_group(p.group_id, "UMAYA")
    assert len(done) == 1 and svc.store.get_requirement("REQ-7").status == "retired" and version == "1.1.0"
