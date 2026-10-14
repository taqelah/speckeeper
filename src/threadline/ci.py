"""CI verification: JUnit results from GitHub Actions, mapped to acceptance criteria by key.

A test proves an AC only when its name starts with that AC's key, e.g.
`AC-7-1@v2 rejects a 12 MB file`. The `@vN` tag says which version of the AC the test was written
for; a pass from a test written for an older version proves nothing about the new text, so the AC
is marked stale instead of passing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .models import Requirement

KEY = re.compile(r"\b(AC-\d+-\d+)(?:@v(\d+))?\b")


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    ac_key: str | None
    ac_version: int | None


def parse_junit(xml_text: str) -> list[TestResult]:
    root = ET.fromstring(xml_text)
    results = []
    for case in root.iter("testcase"):
        name = case.get("name", "")
        failed = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None
        if skipped:
            continue
        m = KEY.search(name)
        results.append(
            TestResult(
                name=name,
                passed=not failed,
                ac_key=m.group(1) if m else None,
                ac_version=int(m.group(2)) if m and m.group(2) else None,
            )
        )
    return results


def apply_results(reqs: list[Requirement], results: list[TestResult], run_id: str) -> tuple[list[Requirement], list[str]]:
    """Update each AC from the run. Returns the requirements that changed and the unmapped test names."""
    by_key = {r.ac_key: r for r in results if r.ac_key}
    changed: list[Requirement] = []
    for req in reqs:
        touched = False
        for ac in req.acs:
            res = by_key.get(ac.key)
            if res is None:
                continue
            touched = True
            ac.last_run = run_id
            ac.last_test = res.name
            if not res.passed:
                ac.ci_status = "failing"
            elif (res.ac_version or 1) < ac.version:
                ac.ci_status = "stale"
            else:
                ac.ci_status = "passing"
        if touched:
            changed.append(req)
    unmapped = [r.name for r in results if r.ac_key is None]
    return changed, unmapped


def summary(req: Requirement, run_id: str) -> str:
    icon = {"passing": ":white_check_mark:", "failing": ":x:", "stale": ":warning:", "none": ":grey_question:"}
    lines = [f"CI run {run_id}. Results for *{req.key}* {req.title}:"]
    for ac in req.acs:
        lines.append(f"{icon[ac.ci_status]} {ac.key} {ac.ci_status}" + (f" · {ac.last_test}" if ac.last_test else ""))
    lines.append(f"*{req.key} is verified.*" if req.verified else f"{req.key} is not verified yet.")
    return "\n".join(lines)
