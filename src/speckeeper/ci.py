"""CI verification: JUnit results from GitHub Actions, mapped to acceptance criteria by key.

A test proves an AC only when its name starts with that AC's key, e.g.
`AC-7-1@v2 rejects a 12 MB file`. The `@vN` tag says which version of the AC the test was written
for; a pass from a test written for an older version proves nothing about the new text, so the AC
is marked stale instead of passing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from defusedxml.ElementTree import fromstring  # no entity expansion or external entities

from .cards import _esc, _label
from .models import Requirement

KEY = re.compile(r"\b(AC-\d+-\d+)(?:@v(\d+))?\b")


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    ac_key: str | None
    ac_version: int | None


def parse_junit(xml_text: str) -> list[TestResult]:
    root = fromstring(xml_text, forbid_dtd=True)
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
    # The title is Gemini text and run_id and test names come from CI: escaped so none can ping or link.
    lines = [f"CI run {_esc(run_id)}. Results for *{req.key}* {_esc(req.title)}:"]
    for ac in req.acs:
        lines.append(f"{icon[ac.ci_status]} {ac.key} {ac.ci_status}" + (f" · {_esc(ac.last_test)}" if ac.last_test else ""))
    lines.append(f"*{req.key} is verified.*" if req.verified else f"{req.key} is not verified yet.")
    return "\n".join(lines)


def report_ok(url: str, repo: str) -> bool:
    """`url` is on the product repo's own GitHub Pages site (the Playwright report with test videos): a
    forged CI post can't put a link of its choosing in front of people."""
    if not repo or "/" not in repo:
        return False
    owner, name = repo.split("/", 1)
    site = f"https://{owner.lower()}.github.io/{name}/"
    return url.startswith(site) and len(url) <= 300 and bool(re.fullmatch(r"[A-Za-z0-9._~/#?=&%:-]+", url))


def github_ok(url: str, repo: str, kind: str) -> bool:
    """`url` is this repo's Actions run (`run`) or pull request (`pull`) on github.com, nothing else."""
    if not repo or "/" not in repo:
        return False
    tail = r"actions/runs/\d+(/attempts/\d+)?" if kind == "run" else r"pull/\d+"
    return bool(re.fullmatch(rf"https://github\.com/{re.escape(repo)}/{tail}", url))


def report_link(url: str, repo: str) -> str:
    return f"\n<{url}|▶ Watch the tests>" if report_ok(url, repo) else ""


def _test(name: str) -> str:
    return "`" + _esc(name).replace("`", "'") + "`"


def slack_summary(req: Requirement, run_id: str, repo: str = "", run_url: str = "", pr_url: str = "", report_url: str = "") -> str:
    """The CI result as Slack shows it: the verdict first, a line per criterion in plain words, then links to
    the run, the PR it came from, the test videos and the issue. Links are used only if they point at the
    product repo (see report_ok, github_ok); CI's text is escaped, so it can't ping or link."""
    failing = any(ac.ci_status == "failing" for ac in req.acs)
    icon = ":white_check_mark:" if req.verified else ":x:" if failing else ":warning:"
    verdict = f"*{req.key} is verified*" if req.verified else f"*{req.key} is not verified yet*"
    lines = [f"{icon} {verdict} (v{req.version}): {_esc(req.title)}"]
    for ac in req.acs:
        test = _test(ac.last_test) if ac.last_test else ""
        if ac.ci_status == "passing":
            lines.append(f":white_check_mark: {ac.key} passes: {test}")
        elif ac.ci_status == "failing":
            lines.append(f":x: {ac.key} fails: {test}")
        elif ac.ci_status == "stale":
            m = re.search(r"@v(\d+)", ac.last_test)
            lines.append(f":warning: {ac.key} stale: {test} proves v{m.group(1) if m else '1'}; rename it @v{ac.version} once it checks v{ac.version}")
        else:
            lines.append(f":grey_question: {ac.key}: no test yet")
    footer = [f"<{run_url}|{_label('CI run ' + run_id)}>" if github_ok(run_url, repo, "run") else f"CI run {_esc(run_id)}"]
    if github_ok(pr_url, repo, "pull"):
        footer.append(f"from <{pr_url}|PR #{pr_url.rsplit('/', 1)[1]}>")
    if report_ok(report_url, repo):
        footer.append(f"<{report_url}|▶ Watch the tests>")
    if repo and req.issue_number:
        footer.append(f"<https://github.com/{repo}/issues/{req.issue_number}|Issue #{req.issue_number}>")
    return "\n".join(lines + [" · ".join(footer)])
