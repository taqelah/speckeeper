"""Meeting notes in, a baseline out: one group of proposals, approved together as PRD v1.0.0."""

from speckeeper.cards import APPROVE_ALL, baseline_card
from speckeeper.notes import doc_id_in, passages
from speckeeper.service import SpecKeeper
from speckeeper.store import MemoryStore

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
            {
                "title": "Receipt uploads are limited to 25 MB",
                "acceptance_criteria": [{"given": "a 30 MB file", "when": "uploaded", "then": "show 'File exceeds 25 MB'"}],
                "source_ts": limit,
            },
            {
                "title": "Every upload is virus-scanned before it is stored",
                "acceptance_criteria": [{"given": "an infected file", "when": "uploaded", "then": "it is rejected"}],
                "source_ts": scan,
            },
        ],
        "open_questions": ["Should photos be compressed on the phone?"],
    }


class FakePRD:
    def __init__(self):
        self.appended, self.notes, self.pages = [], [], []

    def publish(self, reqs, releases, version, now):
        from speckeeper.prd import render

        self.appended = [r.key for r in reqs if r.status == "approved"]
        self.notes = [f"v{x['version']} · {x['what']} · approved by {x['by']}" for x in releases]
        self.pages.append(render("Checkout v2", reqs, releases, version, "acme/checkout", now))


def test_doc_link_is_found_in_a_slack_mention():
    text = f"<@U1> capture <https://docs.google.com/document/d/{DOC}/edit?usp=sharing|kickoff notes>"
    assert doc_id_in(text) == DOC


def test_passages_keep_the_meeting_time():
    ps = passages(DOC, "Checkout v2 kickoff", NOTES)
    limit = next(p for p in ps if "upload size limit" in p.text)
    assert limit.kind == "meeting" and limit.label == "Checkout v2 kickoff, 00:06:45"


def test_meeting_becomes_one_group_and_a_baseline():
    prd = FakePRD()
    svc = SpecKeeper(MemoryStore(first_req=7), generate, prd)
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
    svc = SpecKeeper(MemoryStore(), generate, FakePRD())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    later, _ = svc.capture("C1", "2.1", passages(DOC, "Sync", NOTES))
    _, version = svc.approve(later[0].id, "UMAYA")
    assert version == "1.1.0"


def test_one_by_one_from_the_kickoff_is_still_the_baseline():
    svc = SpecKeeper(MemoryStore(first_req=7), generate, FakePRD())
    _gid, _, proposals, _ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    versions = [svc.approve(p.id, "UMAYA")[1] for p in proposals]
    assert versions == ["1.0.0", "1.0.0"]
    later, _ = svc.capture("C1", "2.1", passages(DOC, "Sync", NOTES))
    assert svc.approve(later[0].id, "UMAYA")[1] == "1.1.0"


def test_capturing_the_same_notes_twice_files_nothing_new():
    svc = SpecKeeper(MemoryStore(), generate, FakePRD())
    assert svc.already_captured(DOC) is None
    gid, _, proposals, _ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    seen_gid, group = svc.already_captured(DOC)
    assert seen_gid == gid and len(group) == len(proposals)


def test_issue_cites_each_meeting_once_with_its_times():
    from speckeeper.integrations import _sources_line

    svc = SpecKeeper(MemoryStore(first_req=7), generate, FakePRD())
    _, _, proposals, _ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    line = _sources_line(proposals[0].requirement, markdown=True)
    assert line == f"Source: [Checkout v2 kickoff](https://docs.google.com/document/d/{DOC}/edit) (00:06:45)"


class FakeIssues:
    def __init__(self):
        self.created, self.updated, self.comments = [], [], []

    def create(self, req):
        self.created.append(req.key)
        return 1

    def update(self, number, req):
        self.updated.append((number, req.version, req.title))

    def comment(self, number, body):
        self.comments.append((number, body))

    def close(self, number, comment):
        self.closed = getattr(self, "closed", []) + [number]


def test_a_change_makes_v2_of_the_same_requirement_and_flags_stale_tests():
    from speckeeper.cards import change_card
    from speckeeper.models import Source

    prd, issues = FakePRD(), FakeIssues()
    calls = []

    def gen(prompt, schema):
        calls.append(prompt)
        if len(calls) == 1:
            return generate(prompt, schema)  # the kickoff
        return {
            "requirements": [
                {
                    "title": "Receipt uploads are limited to 10 MB",
                    "acceptance_criteria": [{"given": "a 12 MB file", "when": "uploaded", "then": "show 'File exceeds 10 MB'"}],
                    "source_ts": ["2.1"],
                    "changes": "req-7",
                }
            ],
            "open_questions": [],
        }

    svc = SpecKeeper(MemoryStore(first_req=7), gen, prd, issues)
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    svc.approve_group(gid, "UMAYA")
    req7 = svc.store.get_requirement("REQ-7")
    req7.acs[0].last_test, req7.acs[0].ci_status = "AC-7-1@v1 rejects a 30 MB file", "passing"
    svc.store.save_requirement(req7)

    thread = [Source(ts="2.1", user="UWEI", text="Storage bill doubled. Let's cap uploads at 10 MB.", label="Wei, Oct 15 16:02")]
    (p,), _ = svc.capture("C1", "2.1", thread)
    assert "REQ-7 | Receipt uploads are limited to 25 MB" in calls[1]  # Gemini sees what is approved
    assert p.kind == "change" and p.target == "REQ-7"
    card = str(change_card(p, svc.store.get_requirement("REQ-7")))
    assert "Change to REQ-7" in card and "- AC-7-1" in card and "+ AC-7-1" in card and "Stale test" in card

    done, version = svc.approve(p.id, "UMAYA", "maya")
    r = done.requirement
    assert (r.key, r.version, version) == ("REQ-7", 2, "1.1.0")
    assert r.acs[0].ci_status == "stale" and r.acs[0].version == 2
    assert {s.ts for s in r.sources} >= {"2.1"} and any(s.kind == "meeting" for s in r.sources)  # both sources kept
    assert svc.store.get_requirement("REQ-12") is None  # no second, contradicting requirement
    assert issues.updated == [(1, 2, "Receipt uploads are limited to 10 MB")]
    assert "Stale until their tests are updated to @v2: AC-7-1" in issues.comments[-1][1]
    assert prd.notes[-1].startswith("v1.1.0 · REQ-7 changed to v2")


def test_a_meeting_is_numbered_in_meeting_order_whatever_order_the_store_returns():
    class ShuffledStore(MemoryStore):
        def proposals_in_group(self, group_id):
            return list(reversed(super().proposals_in_group(group_id)))  # like Firestore, any order

    svc = SpecKeeper(ShuffledStore(first_req=7), generate, FakePRD())
    gid, *_ = svc.capture_meeting("C1", "1.1", DOC, fetch)
    done, _ = svc.approve_group(gid, "UMAYA")
    assert {p.requirement.key: p.requirement.title for p in done}["REQ-7"] == "Receipt uploads are limited to 25 MB"
