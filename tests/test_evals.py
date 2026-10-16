"""The eval cases must be answerable: each expected tool call works on the fixture and returns the facts
the reference answer states. Catches a case drifting from the fixture without spending a Gemini call."""

import json

from evals.qa.cases import CASES
from evals.qa.fixture import fixture_store
from evals.qa.run import CONFIG, eval_set
from speckeeper.agent import build_tools

# Facts each reference answer relies on, which the expected tools must return.
FACTS = {
    "why-limit": ["8 MB", "25 MB", "U_MAYA", "Oct 15, 16:20", "Storage costs tripled", "00:06:45"],
    "limit-before": ["25 MB", '"version": 1', "00:06:45"],
    "who-approved-change": ["U_MAYA", "Oct 15, 16:20", '"version_after": 2'],
    "req7-verified": ['"verified_by_ci": false', "AC-7-1@v1", "AC-7-2@v1", "stale"],
    "verified-list": ['"key": "REQ-8"'],
    "kickoff-list": ["REQ-7", "REQ-8", "REQ-9", "REQ-10", "REQ-11"],
    "req10-criteria": ["AC-10-1", "a log entry records the user, the time and the file", "failing"],
    "req10-failing": ["failing", "AC-10-1@v1 logs a download"],
    "virus-issue": ['"github_issue": 14', "virus-scanned"],
    "resume-decided": ["00:11:20", "mobile networks", "24 hours", "Oct 15, 12:43"],
    "audit-retention": ["one year", "Compliance"],
    "unknown": ["REQ-11"],
    "injection": [],
}


def tool_output(calls):
    tools = {t.__name__: t for t in build_tools(fixture_store())}
    out = [tools[name](**args) for name, args in calls]
    return json.dumps(out, ensure_ascii=False)


def test_cases_are_unique_and_sized():
    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids))
    assert 10 <= len(CASES) <= 15
    assert set(FACTS) == set(ids)


def test_expected_tools_return_the_facts():
    for case_id, _question, calls, _reference in CASES:
        out = tool_output(calls)
        assert '"error"' not in out, case_id
        for fact in FACTS[case_id]:
            assert fact in out, (case_id, fact)


def test_only_req8_is_verified():
    listed = build_tools(fixture_store())[0]()
    assert [r["key"] for r in listed if r["verified_by_ci"]] == ["REQ-8"]


def test_eval_set_matches_cases():
    es = eval_set()
    assert [c.eval_id for c in es.eval_cases] == [c[0] for c in CASES]
    first = es.eval_cases[0].conversation[0]
    assert first.intermediate_data.tool_uses[0].name == "get_history"
    assert set(CONFIG.criteria) == {"tool_trajectory_avg_score", "final_response_match_v2", "hallucinations_v1"}
