"""A fixed project for evaluating the "why?" agent: the Checkout v2 kickoff approved as PRD v1.0.0, then
REQ-7 changed from 25 MB to 8 MB in a Slack thread, with CI results that are passing, failing, stale and
not yet run. Built in memory, so every eval run asks the same questions of the same facts."""

from __future__ import annotations

from speckeeper.models import AcceptanceCriterion, Proposal, Requirement, Source
from speckeeper.store import MemoryStore

NOTES = "https://docs.google.com/document/d/1-OQUq27-jKS0camHFirxQC1jDYX5ZQoK1zuPD88hUX4/edit"
MEETING = "Checkout v2 kickoff · Meeting notes · Oct 14, 2026"
KICKOFF_APPROVED = "2026-10-15T04:43:00+00:00"  # 12:43 SGT
CHANGE_APPROVED = "2026-10-15T08:20:00+00:00"  # 16:20 SGT
MAYA, WEI = "U_MAYA", "U_WEI"


def _meeting(time: str, text: str) -> Source:
    return Source(ts=f"n{time}", user="", text=text, kind="meeting", label=f"{MEETING}, {time}", permalink=NOTES)


def _ac(key: str, given: str, when: str, then: str, version: int = 1, ci: str = "none", test: str = "") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        key=key, gherkin=f"Given {given}\nWhen {when}\nThen {then}", version=version, ci_status=ci, last_test=test, last_run="#41" if test else ""
    )


LIMIT_NOTE = _meeting(
    "00:06:45",
    "Receipt upload size limit: Arjun asked what size of receipt file to accept. Wei said storage can handle 25 MB per file. "
    'Maya decided on a 25 MB limit; anything larger shows the error "File exceeds 25 MB" before the upload starts.',
)
WEI_MESSAGE = Source(
    ts="1792046470.000100",
    user=WEI,
    text="Storage costs tripled this month. Let's cap uploads at 8 MB.",
    kind="slack",
    label="Wei Tan, Oct 15 16:41",
    permalink="https://taqelah.slack.com/archives/C0CA2MM37QQ/p1792046470000100",
)

REQ7_V1 = Requirement(
    key="REQ-7",
    title="Receipt upload size limit is 25 MB",
    status="approved",
    issue_number=12,
    sources=[LIMIT_NOTE],
    acs=[
        _ac("AC-7-1", "the user is on the file picker", "they view the upload options", "the 25 MB limit is shown"),
        _ac(
            "AC-7-2",
            "the user selects a file larger than 25 MB",
            "the upload is about to start",
            "the error 'File exceeds 25 MB' is shown and the upload does not start",
        ),
    ],
)
REQ7_V2 = Requirement(
    key="REQ-7",
    title="Receipt upload size limit is 8 MB",
    version=2,
    status="approved",
    issue_number=12,
    sources=[LIMIT_NOTE, WEI_MESSAGE],
    acs=[
        _ac(
            "AC-7-1",
            "the user is on the file picker",
            "they view the upload options",
            "the 8 MB limit is shown",
            2,
            "stale",
            "AC-7-1@v1 shows the limit in the file picker",
        ),
        _ac(
            "AC-7-2",
            "the user selects a file larger than 8 MB",
            "the upload is about to start",
            "the error 'File exceeds 8 MB' is shown and the upload does not start",
            2,
            "stale",
            "AC-7-2@v1 rejects a 30 MB file before upload",
        ),
    ],
)
OTHERS = [
    Requirement(
        key="REQ-8",
        title="Interrupted uploads resume for up to 24 hours",
        status="approved",
        issue_number=13,
        sources=[
            _meeting(
                "00:11:20",
                "Large files on mobile: Wei raised that uploads near 25 MB often fail on mobile networks. The team agreed uploads must resume "
                "where they stopped after a dropped connection, for up to 24 hours, without the user choosing the file again.",
            )
        ],
        acs=[
            _ac(
                "AC-8-1",
                "an upload is interrupted by a dropped connection",
                "the connection returns within 24 hours",
                "the upload resumes where it stopped without choosing the file again",
                ci="passing",
                test="AC-8-1@v1 resumes after a dropped connection",
            )
        ],
    ),
    Requirement(
        key="REQ-9",
        title="Every uploaded file is virus-scanned before it is stored",
        status="approved",
        issue_number=14,
        sources=[
            _meeting(
                "00:15:05",
                "Virus scan: Priya said Payments requires every uploaded file to be scanned before it is stored. The team agreed: an infected "
                'file is rejected with "This file can\'t be uploaded" and is never saved.',
            )
        ],
        acs=[
            _ac("AC-9-1", "a user uploads an infected file", "the scan finishes", 'the error "This file can\'t be uploaded" is shown and the file is not saved')
        ],
    ),
    Requirement(
        key="REQ-10",
        title="Every receipt download is logged with user, time and file",
        status="approved",
        issue_number=15,
        sources=[
            _meeting(
                "00:19:30",
                "Audit log for downloads: Priya asked for a record of who downloads a receipt. Agreed: every download is logged with the user, "
                "the time and the file. Priya proposed keeping the log for one year; Maya wants Compliance to confirm the period before it is final.",
            )
        ],
        acs=[
            _ac(
                "AC-10-1",
                "a user downloads a receipt",
                "the download completes",
                "a log entry records the user, the time and the file",
                ci="failing",
                test="AC-10-1@v1 logs a download",
            )
        ],
    ),
    Requirement(
        key="REQ-11",
        title="A saved card pays in one step",
        status="approved",
        issue_number=16,
        sources=[
            _meeting(
                "00:24:10",
                "Saved cards: Maya confirmed that a user with a saved card pays in one step: tapping Pay places the order with no extra screen.",
            )
        ],
        acs=[_ac("AC-11-1", "a user has a saved card", "they tap Pay", "the order is placed with no extra screen")],
    ),
]


def fixture_store() -> MemoryStore:
    store = MemoryStore(first_req=7)
    for seq, req in enumerate([REQ7_V1, *OTHERS]):
        store.save_proposal(
            Proposal(
                id=f"p-{req.key}",
                kind="new",
                requirement=req,
                channel="C0CA2MM37QQ",
                thread_ts="1792040000.000100",
                status="approved",
                decided_by=MAYA,
                decided_at=KICKOFF_APPROVED,
                group_id="g-kickoff",
                created_at=KICKOFF_APPROVED,
                seq=seq,
            )
        )
    store.save_proposal(
        Proposal(
            id="p-REQ-7-v2",
            kind="change",
            target="REQ-7",
            requirement=REQ7_V2,
            previous=REQ7_V1,
            channel="C0CA2MM37QQ",
            thread_ts="1792040000.000100",
            status="approved",
            decided_by=MAYA,
            decided_at=CHANGE_APPROVED,
            created_at=CHANGE_APPROVED,
        )
    )
    for req in [REQ7_V2, *OTHERS]:
        store.save_requirement(req)
    store.set_meta("prd_version", "1.1.0")
    return store
