"""Meeting notes in, a baseline out: one group of proposals, approved together as PRD v1.0.0."""

from threadline.cards import APPROVE_ALL, baseline_card
from threadline.notes import doc_id_in, passages
from threadline.service import Threadline
from threadline.store import MemoryStore

DOC = "1-OQUq27-jKS0camHFirxQC1jDYX5ZQoK1zuPD88hUX4"
NOTES = """Checkout v2 kickoff
Details
* Receipt upload size limit: Maya decided on a 25 MB limit; larger files show "File exceeds 25 MB". (00:06:45)
* Virus scan: every upload is scanned before it is stored. (00:15:05)
* Image compression: Wei suggested compressing photos. No decision. (00:33:15)
Decisions
* Receipt uploads are limited to 25 MB.
"""


def fetch(doc_id):
    assert doc_id == DOC
    return "Checkout v2 kickoff", NOTES


def generate(prompt, schema):
    ids = {line.split(" | ")[2][:30]: line.split(" | ")[0] for line in prompt.splitlines() if " | notes | " in line}
    limit = [ts for text, ts in ids.items() if "upload size limit" in text or "limited to 25 MB" in text]
    scan = [ts for text, ts in ids.items() if text.startswith("Virus scan")]
    return {
        "requirements": [
            {"title": "Receipt uploads are limited to 25 MB", "acceptance_criteria": [{"given": "a 30 MB file", "when": "uploaded", "then": "show 'File exceeds 25 MB'"}], "source_ts": limit},
            {"title": "Every upload is virus-scanned before it is stored", "acceptance_criteria": [{"given": "an infected file", "when": "uploaded", "then": "it is rejected"}], "source_ts": scan},
        ],
        "open_questions": ["Should photos be compressed on the phone?"],
    }


class FakePRD:
    def __init__(self):
        self.appended, self.notes = [], []

    def append(self, req):
        self.appended.append(req.key)

    def note(self, text):
        self.notes.append(text)


def test_doc_link_is_found_in_a_slack_mention():
    text = f"<@U1> capture <https://docs.google.com/document/d/{DOC}/edit?usp=sharing|kickoff notes>"
    assert doc_id_in(text) == DOC


def test_passages_keep_the_meeting_time():
    ps = passages(DOC, "Checkout v2 kickoff", NOTES)
    limit = next(p for p in ps if "upload size limit" in p.text)
    assert limit.kind == "meeting" and limit.label == "Checkout v2 kickoff, 00:06:45"


def test_meeting_becomes_one_group_and_a_baseline():
    prd = FakePRD()
    svc = Threadline(MemoryStore(first_req=7), generate, prd)
    gid, title, proposals, questions = svc.capture_meeting("C1", "1.1", DOC, fetch)
    assert title == "Checkout v2 kickoff" and len(proposals) == 2 and questions
    card = str(baseline_card(gid, title, proposals, questions))
    assert "captured 2 agreed requirements" in card and "00:06:45" in card and APPROVE_ALL in card

    done, version = svc.approve_group(gid, "UMAYA", "maya")
    assert version == "1.0.0"
    assert [p.requirement.key for p in done] == ["REQ-7", "REQ-8"]
    assert prd.appended == ["REQ-7", "REQ-8"]
    assert prd.notes == ["v1.0.0 · 2 requirements from the kickoff (REQ-7, REQ-8) · approved by maya"]
    assert svc.approve_group(gid, "UMAYA") == ([], "1.0.0")  # approving twice files nothing


def test_a_later_approval_is_a_minor_version():
    svc = Threadline(MemoryStore(), generate, FakePRD())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    later, _ = svc.capture("C1", "2.1", passages(DOC, "Sync", NOTES))
    _, version = svc.approve(later[0].id, "UMAYA")
    assert version == "1.1.0"


def test_one_by_one_from_the_kickoff_is_still_the_baseline():
    svc = Threadline(MemoryStore(first_req=7), generate, FakePRD())
    gid, _, proposals, _ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    versions = [svc.approve(p.id, "UMAYA")[1] for p in proposals]
    assert versions == ["1.0.0", "1.0.0"]
    later, _ = svc.capture("C1", "2.1", passages(DOC, "Sync", NOTES))
    assert svc.approve(later[0].id, "UMAYA")[1] == "1.1.0"


def test_capturing_the_same_notes_twice_files_nothing_new():
    svc = Threadline(MemoryStore(), generate, FakePRD())
    assert svc.already_captured(DOC) is None
    gid, _, proposals, _ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    seen_gid, group = svc.already_captured(DOC)
    assert seen_gid == gid and len(group) == len(proposals)
