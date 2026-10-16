from fakes import FakeEngine
from test_meeting import DOC, FakeIssues, FakePRD, fetch, generate

from speckeeper.models import AcceptanceCriterion, Requirement, Source
from speckeeper.prd import render, source_parts, steps
from speckeeper.service import SpecKeeper
from speckeeper.store import MemoryStore

LABEL = "Checkout v2 kickoff · Meeting notes · Oct 14, 2026, 00:06:45"


def req(**kw):
    base = dict(
        key="REQ-7",
        title="Receipt upload size limit is 25 MB",
        status="approved",
        issue_number=18,
        acs=[
            AcceptanceCriterion(
                key="AC-7-1", gherkin="Given a 30 MB file\nAnd the user is signed in\nWhen they upload it\nThen show 'File exceeds 25 MB'", ci_status="passing"
            )
        ],
        sources=[Source(ts="n1", user="", text="x", kind="meeting", label=LABEL, permalink="https://docs.google.com/document/d/D/edit")],
    )
    base.update(kw)
    return Requirement(**base)


def test_a_meeting_title_with_a_comma_keeps_its_date_and_time_apart():
    assert source_parts(req()) == [("Checkout v2 kickoff · Meeting notes · Oct 14, 2026", " (00:06:45)", "https://docs.google.com/document/d/D/edit")]


def test_a_meeting_label_without_a_time_is_one_source_not_two():
    r = req(
        sources=req().sources
        + [
            Source(
                ts="n2",
                user="",
                text="y",
                kind="meeting",
                label="Checkout v2 kickoff · Meeting notes · Oct 14, 2026",
                permalink="https://docs.google.com/document/d/D/edit",
            )
        ]
    )
    assert source_parts(r) == [("Checkout v2 kickoff · Meeting notes · Oct 14, 2026", " (00:06:45)", "https://docs.google.com/document/d/D/edit")]


def test_and_lines_join_the_step_before_them():
    assert steps("Given a 30 MB file\nAnd the user is signed in\nWhen they upload it\nThen show it") == {
        "Given": ["a 30 MB file", "and the user is signed in"],
        "When": ["they upload it"],
        "Then": ["show it"],
    }


def test_the_page_has_a_summary_criteria_tables_and_history():
    page = render(
        "Checkout v2",
        [req(), req(key="REQ-8", title="Old", status="dismissed")],
        [{"version": "1.0.0", "what": "kickoff", "by": "Syam", "at": "2026-10-15T04:43:00+00:00"}],
        "1.0.0",
        "acme/checkout",
        "2026-10-15T04:43:00+00:00",
    )
    assert "Summary" in page and "Change history" in page and '<b style="color:#2f6fde">v1.0.0</b>' in page
    assert 'href="https://github.com/acme/checkout/issues/18"' in page and "All 1 passing" in page
    assert "15 Oct 2026, 12:43 SGT" in page and "Old" not in page  # only approved requirements
    assert "and the user is signed in" in page and "(00:06:45)" in page


def test_text_from_meetings_is_escaped_and_odd_links_are_not_linked():
    bad = req(title="<script>x</script>", sources=[Source(ts="1", user="U", text="x", label="Maya", permalink="javascript:alert(1)")])
    page = render("T", [bad], [], "1.0.0")
    assert "<script>x" not in page and "&lt;script&gt;" in page and "javascript:" not in page


class BrokenPRD(FakePRD):
    def publish(self, *a):
        raise RuntimeError("Docs is down")


def test_a_prd_failure_does_not_undo_the_approval():
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(generate), BrokenPRD(), FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    done, version = svc.approve_group(gid, "U1", "maya")
    assert version == "1.0.0" and [p.requirement.key for p in done] == ["REQ-7", "REQ-8"]
    assert svc.publish_prd() is False


def test_a_ci_run_rebuilds_the_prd_with_the_results():
    prd = FakePRD()
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(generate), prd, FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "U1", "maya")
    before = len(prd.pages)
    svc.take_ci('<testsuites><testsuite><testcase name="AC-7-1@v1 shows the limit"/></testsuite></testsuites>', "#1")
    assert len(prd.pages) == before + 1 and "Passing" in prd.pages[-1]
