"""The loop without Slack, Gemini, Docs or GitHub: fakes stand in for all of them."""

from __future__ import annotations

from threadline.cards import APPROVE, approval_card
from threadline.ci import parse_junit
from threadline.extract import PROMPT
from threadline.models import Source
from threadline.service import Threadline
from threadline.store import MemoryStore

THREAD = [
    Source(ts="1.1", user="UMAYA", text="For v2, let's allow uploads up to 25 MB. Anything bigger gets a clear error."),
    Source(ts="1.2", user="UARJUN", text="Works for me. I'll show the limit in the file picker too."),
]

EXTRACTION = {
    "requirements": [
        {
            "title": "File upload size limit is 25 MB",
            "acceptance_criteria": [
                {"given": "a 30 MB file", "when": "the user uploads it", "then": "show 'File exceeds 25 MB'"},
                {"given": "a 20 MB file", "when": "the user uploads it", "then": "the upload succeeds"},
            ],
            "source_ts": ["1.1", "1.2"],
        },
        {"title": "Invented requirement", "acceptance_criteria": [{"given": "x", "when": "y", "then": "z"}], "source_ts": ["9.9"]},
    ],
    "open_questions": ["Does the limit apply to images?"],
}


class FakePRD:
    def __init__(self):
        self.appended, self.notes = [], []

    def append(self, req):
        self.appended.append(req.key)

    def note(self, text):
        self.notes.append(text)


class FakeIssues:
    def __init__(self):
        self.created, self.comments = [], []

    def create(self, req):
        self.created.append(req.key)
        return 42

    def comment(self, number, body):
        self.comments.append((number, body))


def make():
    prompts = []

    def generate(prompt, schema):
        prompts.append(prompt)
        return EXTRACTION

    prd, issues = FakePRD(), FakeIssues()
    return Threadline(MemoryStore(), generate, prd, issues), prompts, prd, issues


def test_capture_keeps_only_cited_requirements():
    svc, prompts, *_ = make()
    proposals, questions = svc.capture("C1", "1.1", THREAD)
    assert [p.requirement.title for p in proposals] == ["File upload size limit is 25 MB"]
    assert [s.ts for s in proposals[0].requirement.sources] == ["1.1", "1.2"]
    assert questions == ["Does the limit apply to images?"]
    assert "1.1 | UMAYA | For v2" in prompts[0]


def test_card_has_gherkin_sources_and_buttons():
    svc, *_ = make()
    (p,), _ = svc.capture("C1", "1.1", THREAD)
    blocks = approval_card(p)
    text = str(blocks)
    assert "1 requirement and 2 acceptance criteria" in text
    assert "Given a 30 MB file" in text and "<@UMAYA>" in text
    assert blocks[-1]["elements"][0]["action_id"] == APPROVE


def test_approve_writes_once_with_keys():
    svc, _, prd, issues = make()
    (p,), _ = svc.capture("C1", "1.1", THREAD)
    done, version = svc.approve(p.id, "UMAYA", "maya")
    assert version == "1.0.0" and prd.notes == ["v1.0.0 · REQ-1 File upload size limit is 25 MB · approved by maya"]
    assert done.requirement.key == "REQ-1"
    assert [ac.key for ac in done.requirement.acs] == ["AC-1-1", "AC-1-2"]
    assert done.requirement.issue_number == 42
    assert prd.appended == ["REQ-1"] and issues.created == ["REQ-1"]
    assert svc.approve(p.id, "UMAYA") == (None, "1.0.0")  # a double click files nothing twice
    assert prd.appended == ["REQ-1"]


def test_dismiss_writes_nothing():
    svc, _, prd, issues = make()
    (p,), _ = svc.capture("C1", "1.1", THREAD)
    assert svc.dismiss(p.id, "UMAYA").status == "dismissed"
    assert prd.appended == [] and issues.created == []


JUNIT = """<testsuites><testsuite name="upload">
<testcase name="AC-1-1@v1 rejects a 30 MB file"/>
<testcase name="AC-1-2@v1 accepts a 20 MB file"><failure message="timeout"/></testcase>
<testcase name="renders the page"/>
</testsuite></testsuites>"""


def test_ci_maps_tests_to_acs_and_comments_on_the_issue():
    svc, _, _, issues = make()
    (p,), _ = svc.capture("C1", "1.1", THREAD)
    svc.approve(p.id, "UMAYA")
    changed, unmapped = svc.take_ci(JUNIT, "#128")
    (req, text), = changed
    assert [ac.ci_status for ac in req.acs] == ["passing", "failing"]
    assert not req.verified and "not verified" in text
    assert unmapped == ["renders the page"]
    assert issues.comments[0][0] == 42


def test_a_pass_from_an_older_version_is_stale():
    svc, *_ = make()
    (p,), _ = svc.capture("C1", "1.1", THREAD)
    svc.approve(p.id, "UMAYA")
    req = svc.store.get_requirement("REQ-1")
    req.acs[0].version = 2  # the AC changed after drift; the test still says v1
    svc.store.save_requirement(req)
    (req, _), = svc.take_ci(JUNIT, "#129")[0]
    assert req.acs[0].ci_status == "stale"


def test_junit_parsing_reads_keys_and_versions():
    (a, b, c) = parse_junit(JUNIT)
    assert (a.ac_key, a.ac_version, a.passed) == ("AC-1-1", 1, True)
    assert (b.passed, c.ac_key) == (False, None)


def test_prompt_forbids_uncited_items():
    assert "Never cite a ts that is not below" in PROMPT
