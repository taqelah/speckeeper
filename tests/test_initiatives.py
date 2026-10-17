"""Initiatives: the living product spec plus a PRD per initiative ("Checkout v2"), both from the same records.

The story: what Checkout does today is captured and approved as the baseline (v1.0.0). The PM links the
Checkout v2 PRD. The v2 kickoff is read against the baseline: one new requirement and a change to REQ-7,
approved together as v2.0.0. The spec has everything; the PRD has what v2 adds and changes."""

import json

import pytest
from fakes import FakeEngine
from test_e2e import LINK, App, settings
from test_meeting import DOC, NOTES, FakeIssues, FakePRD, generate

from speckeeper.models import Source
from speckeeper.service import SpecKeeper
from speckeeper.store import MemoryStore

V2_DOC = "1-V2kickoffNotesDocId00000000000000000000000"
V2_LINK = f"<https://docs.google.com/document/d/{V2_DOC}/edit|v2 kickoff>"
PRD_DOC = "1-CheckoutV2PrdDocId000000000000000000000000"
PRD_LINK = f"<https://docs.google.com/document/d/{PRD_DOC}/edit|Checkout v2 PRD>"
V2_NOTES = """Checkout v2 kickoff
Details
* Resumable uploads: an interrupted upload resumes for up to 24 hours. (00:04:10)
* Upload cap: storage costs doubled, so uploads are capped at 8 MB from now on. (00:09:30)
"""


def cites(prompt, word):
    return [line.split(" | ")[0] for line in prompt.splitlines() if " | notes | " in line and word in line]


def gemini(prompt, schema):
    """The baseline answers as the kickoff did; the v2 kickoff with one new requirement and a change to REQ-7."""
    if "Resumable uploads" not in prompt:
        return generate(prompt, schema)
    return {
        "requirements": [
            {
                "title": "Interrupted uploads resume for up to 24 hours",
                "acceptance_criteria": [{"given": "a dropped upload", "when": "the connection returns within 24 hours", "then": "it resumes"}],
                "source_ts": cites(prompt, "Resumable"),
            },
            {
                "title": "Receipt uploads are limited to 8 MB",
                "acceptance_criteria": [{"given": "a 10 MB file", "when": "uploaded", "then": "show 'File exceeds 8 MB'"}],
                "source_ts": cites(prompt, "Upload cap"),
                "changes": "REQ-7",
            },
        ],
        "open_questions": [],
    }


def fetch(doc_id):
    return ("Checkout v2 kickoff", V2_NOTES) if doc_id == V2_DOC else ("Checkout today", NOTES)


class PagesPRD(FakePRD):
    """The product spec (publish, from FakePRD) and initiative PRDs (publish_page), per doc."""

    def __init__(self, fail=False):
        super().__init__()
        self.docs, self.fail = {}, fail

    def publish_page(self, doc_id, page):
        if self.fail:
            raise RuntimeError("403 the caller does not have permission")
        self.docs.setdefault(doc_id, []).append(page)


def baseline(prd=None, issues=None):
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(gemini), prd or PagesPRD(), issues or FakeIssues())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA", "maya")
    return svc


def kickoff(svc):
    result = svc.capture_notes("C1", "5.1", V2_DOC, fetch)
    assert result.status == "new"
    return result


def section(page, heading):
    """The text of one <h1> section of a rendered page, up to the next <h1>."""
    start = page.index(f"<h1>{heading}</h1>")
    end = page.find("<h1>", start + 4)
    return page[start : end if end > 0 else None]


def test_I_01_a_meeting_on_top_of_the_baseline_is_the_next_major_version_and_lands_in_the_open_prd():
    prd = PagesPRD()
    svc = baseline(prd)
    assert svc.prd_version == "1.0.0" and svc.store.get_requirement("REQ-7").initiative == ""
    svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    assert "Nothing approved for Checkout v2 yet" in prd.docs[PRD_DOC][-1]  # written at once, before any approval

    result = kickoff(svc)
    assert {p.kind for p in result.proposals} == {"new", "change"}
    assert svc.next_version(result.group_id) == "2.0.0"
    done, version = svc.approve_group(result.group_id, "UMAYA", "maya")
    assert version == "2.0.0" and len(done) == 2 and all(p.initiative == "Checkout v2" for p in done)
    assert svc.store.get_requirement("REQ-9").initiative == "Checkout v2"  # the new one
    assert svc.store.get_requirement("REQ-7").version == 2 and svc.store.get_requirement("REQ-7").initiative == ""  # still the baseline's
    assert svc.releases[-1]["initiative"] == "Checkout v2"

    page = prd.docs[PRD_DOC][-1]
    assert "Checkout v2 · PRD" in page
    changes = section(page, "Changes to current behaviour")
    assert "REQ-7" in changes and "8 MB" in changes and "Until now (v1): Receipt uploads are limited to 25 MB" in changes
    assert "REQ-9" in section(page, "New requirements") and "REQ-7" not in section(page, "New requirements")
    context = section(page, "Current behaviour (context)")
    assert "REQ-8" in context and "REQ-7" not in context  # the virus scan: shipped, unchanged by v2
    assert prd.appended == ["REQ-7", "REQ-8", "REQ-9"]  # the spec has everything


def test_I_02_one_by_one_from_the_kickoff_is_still_one_major_version():
    svc = baseline()
    svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    result = kickoff(svc)
    versions = [svc.approve(p.id, "UMAYA")[1] for p in result.proposals]
    assert versions == ["2.0.0", "2.0.0"]


def test_I_03_a_thread_change_during_an_initiative_is_minor_and_in_its_prd():
    prd = PagesPRD()
    svc = baseline(prd)
    svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    svc.approve_group(kickoff(svc).group_id, "UMAYA")

    def thread_change(prompt, schema):
        return {
            "requirements": [
                {
                    "title": "Every upload is virus-scanned before it is stored, within 5 seconds",
                    "acceptance_criteria": [{"given": "a file", "when": "uploaded", "then": "it is scanned within 5 seconds"}],
                    "source_ts": ["9.1"],
                    "changes": "REQ-8",
                }
            ],
            "open_questions": [],
        }

    svc.engine.generate = thread_change
    (p,), _ = svc.capture("C1", "9.1", [Source(ts="9.1", user="UWEI", text="Scans must finish in 5 s.", label="Wei, Oct 17 10:00")])
    assert svc.approve(p.id, "UMAYA")[1] == "2.1.0"
    assert "REQ-8" in section(prd.docs[PRD_DOC][-1], "Changes to current behaviour")


def test_I_04_edited_baseline_notes_stay_a_minor_version():
    svc = baseline()
    edited = NOTES + "* HEIC photos: agreed to accept HEIC receipts. (00:41:00)\n"

    def heic(prompt, schema):
        heic_ts = cites(prompt, "HEIC")
        return {
            "requirements": [
                {
                    "title": "HEIC receipts are accepted",
                    "acceptance_criteria": [{"given": "a HEIC photo", "when": "uploaded", "then": "it is accepted"}],
                    "source_ts": heic_ts,
                }
            ],
            "open_questions": [],
        }

    svc.engine.generate = heic
    result = svc.capture_notes("C1", "1.1", DOC, lambda d: ("Checkout today", edited))
    assert result.status == "changed"
    assert svc.approve(result.proposals[0].id, "UMAYA")[1] == "1.1.0"  # an edit, not a new meeting


def test_I_05_without_an_initiative_a_new_meeting_still_goes_into_the_spec():
    prd = PagesPRD()
    svc = baseline(prd)
    done, version = svc.approve_group(kickoff(svc).group_id, "UMAYA")
    assert version == "2.0.0" and all(p.initiative == "" for p in done) and prd.docs == {}


def test_I_06_retiring_during_an_initiative_shows_in_its_prd():
    prd = PagesPRD()
    svc = baseline(prd)
    svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    (ask,) = svc.request_retire(["REQ-8"], "dropped from v2", "C1", "9.1", "UPM")
    svc.retire_group(ask.group_id, "UMAYA")
    assert "REQ-8" in section(prd.docs[PRD_DOC][-1], "Retired")


def test_N_52_a_prd_doc_spec_keeper_cannot_write_to_is_not_linked():
    svc = baseline(PagesPRD(fail=True))
    with pytest.raises(RuntimeError):
        svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    assert svc.initiatives == []


def test_C_55_linking_the_same_name_again_moves_it_and_reset_keeps_the_link():
    prd = PagesPRD()
    svc = baseline(prd)
    svc.link_initiative("Checkout v2", "OLD", "UPM")
    svc.link_initiative("checkout  v2", PRD_DOC, "UPM")
    assert [(i["name"], i["doc_id"]) for i in svc.initiatives] == [("checkout v2", PRD_DOC)]
    svc.reset_demo("UPM")
    assert [i["doc_id"] for i in svc.initiatives] == [PRD_DOC]  # still linked
    assert "Nothing approved for checkout v2 yet" in prd.docs[PRD_DOC][-1]  # and rebuilt empty


def test_C_56_an_initiative_linked_before_the_baseline_does_not_claim_it():
    prd = PagesPRD()
    svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(gemini), prd, FakeIssues())
    svc.link_initiative("Checkout v2", PRD_DOC, "UPM")
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    done, version = svc.approve_group(gid, "UMAYA")
    assert version == "1.0.0" and all(p.initiative == "" for p in done)


# --- in Slack -------------------------------------------------------------------------------------------


def both_docs(doc_id):
    from test_e2e import fetch_ok

    return fetch(doc_id) if doc_id == V2_DOC else fetch_ok(doc_id)


def slack_app(**changes):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from speckeeper.main import create_app

    app = App(fetch=both_docs)
    app.svc.engine.generate = gemini
    app.prd.doc_id = "SPEC"
    app.prd.docs = {}
    app.prd.publish_page = lambda doc_id, page: app.prd.docs.setdefault(doc_id, []).append(page)
    if changes:
        app.http = TestClient(create_app(replace(settings(), **changes), app.svc, slack_client=app.slack.client(), fetch_doc=both_docs, wait=True))
    return app


def test_E2E_14_link_a_prd_then_the_kickoff_card_offers_v2_and_the_note_links_both_docs():
    app = slack_app()
    app.mention(f"capture {LINK}")
    first = app.last_card()
    assert "Approve all as v1.0.0" in json.dumps(first["blocks"])
    app.click("approve_group", app.button(first, "approve_group"), first)

    app.mention(f"prd Checkout v2 {PRD_LINK}", ts="300.1")
    linked = app.slack.posted("chat.postMessage")[-1]["text"]
    assert f"<https://docs.google.com/document/d/{PRD_DOC}/edit|the Checkout v2 PRD>" in linked and "keeps every requirement" in linked

    app.mention(V2_LINK, ts="400.1")
    card = app.last_card()
    text = json.dumps(card["blocks"])
    assert "1 new requirement and 1 change to approved ones" in text and "Changes *REQ-7*" in text and "Approve all as v2.0.0" in text
    app.click("approve_group", app.button(card, "approve_group"), card)
    note = app.slack.posted("chat.update")[-1]["text"]
    assert "*PRD v2.0.0*" in note and "the product spec>" in note and f"{PRD_DOC}/edit|the Checkout v2 PRD>" in note

    app.mention("prd", ts="500.1")
    assert "Approvals now go into" in app.slack.posted("chat.postMessage")[-1]["text"]


def test_N_53_only_approvers_link_a_prd_and_a_name_is_needed():
    app = slack_app(approvers=("UBOSS",))
    app.mention(f"prd Checkout v2 {PRD_LINK}", ts="300.1")
    assert "<@UBOSS>" in app.slack.posted("chat.postEphemeral")[-1]["text"] and app.svc.initiatives == []
    app = slack_app()
    app.mention(f"prd {PRD_LINK}", ts="300.1")
    assert "Give the initiative a name" in app.slack.posted("chat.postEphemeral")[-1]["text"] and app.svc.initiatives == []


def test_N_54_a_doc_it_cannot_write_to_says_how_to_share_it():
    app = slack_app()

    def denied(doc_id, page):
        raise RuntimeError("403")

    app.prd.publish_page = denied
    app.mention(f"prd Checkout v2 {PRD_LINK}", ts="300.1")
    assert "Share it with Spec Keeper's service account as an Editor" in app.slack.posted("chat.postMessage")[-1]["text"]
    assert app.svc.initiatives == []


def test_C_57_prd_without_a_link_says_how_to_start_one():
    app = slack_app()
    app.mention("prd", ts="300.1")
    assert "No initiative PRD linked yet" in app.slack.posted("chat.postMessage")[-1]["text"]
