"""The PRD as one HTML page, rebuilt from the store on every change and imported into the Google Doc.

Rebuilding (not appending) keeps the doc tidy: a summary table, one section per requirement with its
criteria as a table, and the change history. Google Docs keeps the layout when it imports HTML."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import AcceptanceCriterion, Requirement

TZ = ZoneInfo("Asia/Singapore")
BRAND, INK, MUTED, LINE, HEAD = "#2f6fde", "#1f2328", "#5f6368", "#dadce0", "#f1f3f4"
CI = {
    "passing": ("Passing", "#1a7f37", "#dafbe1"),
    "failing": ("Failing", "#cf222e", "#ffebe9"),
    "stale": ("Stale", "#9a6700", "#fff8c5"),
    "none": ("No test yet", MUTED, "#ffffff"),
}


def h(text: object) -> str:
    return html.escape(str(text), quote=True)


def _when(iso: str, with_time: bool = True) -> str:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return iso
    return t.strftime("%-d %b %Y, %H:%M SGT" if with_time else "%-d %b %Y")


def steps(gherkin: str) -> dict[str, list[str]]:
    """Given / When / Then, with "And" lines joined to the step before them."""
    out: dict[str, list[str]] = {"Given": [], "When": [], "Then": []}
    current = "Given"
    for line in (s.strip() for s in gherkin.splitlines()):
        if not line:
            continue
        word, _, rest = line.partition(" ")
        if word.capitalize() in out:
            current = word.capitalize()
            out[current].append(rest)
        elif word.lower() in ("and", "but"):
            out[current].append(f"{word.lower()} {rest}")
        else:
            out[current].append(line)
    return out


def meeting_label(label: str) -> tuple[str, str]:
    """ "Kickoff · Oct 14, 2026, 00:06:45" -> ("Kickoff · Oct 14, 2026", "00:06:45"): only a clock time at the
    end is the time, not the date's comma. No time: (label, "")."""
    m = re.match(r"(.*), (\d{1,2}:\d{2}(?::\d{2})?)$", label)
    return (m.group(1), m.group(2)) if m else (label, "")


def source_parts(req: Requirement) -> list[tuple[str, str, str]]:
    """(label, times, link) per meeting (its times together) or Slack message, never the same one twice."""
    meetings: dict[str, tuple[str, list[str]]] = {}
    others: list[tuple[str, str, str]] = []
    for s in req.sources:
        if s.kind == "meeting":
            title, time = meeting_label(s.label)
            _link_kept, times = meetings.setdefault(title, (s.permalink, []))
            if time and time not in times:
                times.append(time)
        else:
            item = (s.label or f"{s.user} {s.ts}", "", s.permalink)
            if item not in others:
                others.append(item)
    return [(t, f" ({', '.join(sorted(times))})" if times else "", link) for t, (link, times) in meetings.items()] + others


def _link(text: str, url: str) -> str:
    return f'<a href="{h(url)}" style="color:{BRAND}">{h(text)}</a>' if url.startswith("https://") else h(text)


def _ci_cell(ac: AcceptanceCriterion, version: int) -> str:
    label, color, bg = CI.get(ac.ci_status, CI["none"])
    if ac.ci_status == "stale":
        label = f"Stale: test is for an older version, needs @v{ac.version}"
    return f'<td style="{_td()};background:{bg};color:{color};font-weight:bold">{h(label)}</td>'


def _td(width: str = "") -> str:
    return f"border:1px solid {LINE};padding:6pt 8pt;vertical-align:top" + (f";width:{width}" if width else "")


def _th(width: str = "") -> str:
    return _td(width) + f";background:{HEAD};font-weight:bold;color:{INK}"


def _tests(req: Requirement) -> tuple[str, str]:
    total = len(req.acs)
    counts = {k: sum(1 for a in req.acs if a.ci_status == k) for k in CI}
    if total and counts["passing"] == total:
        return f"All {total} passing", CI["passing"][1]
    if counts["failing"]:
        return f"{counts['failing']} of {total} failing", CI["failing"][1]
    if counts["stale"]:
        return f"{counts['stale']} of {total} stale", CI["stale"][1]
    if counts["passing"]:
        return f"{counts['passing']} of {total} passing", INK
    return "No tests yet", MUTED


def _issue_link(repo: str):
    def issue(n):
        return _link(f"#{n}", f"https://github.com/{repo}/issues/{n}") if n and repo else (f"#{n}" if n else "–")

    return issue


def _head(title: str, kind: str, version: str, count: int, now: str, note: str) -> list[str]:
    return [
        f'<html><head><meta charset="utf-8"></head><body style="font-family:Arial;font-size:11pt;color:{INK}">',
        f'<p style="font-size:26pt;font-weight:bold;margin:0">{h(title)}</p>',
        f'<p style="font-size:13pt;color:{MUTED};margin-top:4pt">{h(kind)} · <b style="color:{BRAND}">v{h(version or "–")}</b>'
        f" · {count} requirement{'s' if count != 1 else ''}" + (f" · updated {h(_when(now))}" if now else "") + "</p>",
        f'<table style="border-collapse:collapse;width:100%"><tr><td style="border:1px solid #c6d8f7;border-left:3pt solid {BRAND};background:#eef4fd;padding:8pt 10pt;color:{INK}">'
        f"{h(note)} To change a requirement, agree the change in Slack and approve it there: this page is rebuilt on every "
        "approval and CI run, so edits made here are replaced.</td></tr></table>",
    ]


def _summary(reqs: list[Requirement], issue) -> list[str]:
    out = [
        '<table style="border-collapse:collapse;width:100%"><tr>'
        f'<th style="{_th("64pt")}">ID</th><th style="{_th()}">Requirement</th><th style="{_th("40pt")}">Ver.</th>'
        f'<th style="{_th("96pt")}">Tests in CI</th><th style="{_th("48pt")}">Issue</th></tr>'
    ]
    for r in reqs:
        tests, color = _tests(r)
        out.append(
            f'<tr><td style="{_td()};font-weight:bold;color:{BRAND}">{h(r.key)}</td><td style="{_td()}">{h(r.title)}</td>'
            f'<td style="{_td()}">v{r.version}</td><td style="{_td()};color:{color}">{h(tests)}</td><td style="{_td()}">{issue(r.issue_number)}</td></tr>'
        )
    out.append("</table>")
    return out


def _requirement(r: Requirement, issue, before: Requirement | None = None) -> list[str]:
    """One requirement with its criteria table; `before` (for a change) adds how it read until then."""
    out = [f'<h2><span style="color:{BRAND}">{h(r.key)}</span> · {h(r.title)}</h2>']
    meta = [f"Version {r.version}"] + ([f"Issue {issue(r.issue_number)}"] if r.issue_number else [])
    sources = source_parts(r)
    if sources:
        meta.append("Agreed in " + ", ".join(_link(label, link) + h(times) for label, times, link in sources))
    out.append(f'<p style="color:{MUTED};font-size:10pt">' + " · ".join(meta) + "</p>")
    if before is not None and before.version != r.version:
        out.append(f'<p style="color:{MUTED}">Until now (v{before.version}): {h(before.title)}</p>')
    out.append(
        '<table style="border-collapse:collapse;width:100%"><tr>'
        f'<th style="{_th("56pt")}">ID</th><th style="{_th()}">Given</th><th style="{_th()}">When</th>'
        f'<th style="{_th()}">Then</th><th style="{_th("72pt")}">CI</th></tr>'
    )
    for ac in r.acs:
        s = steps(ac.gherkin)
        cells = "".join(f'<td style="{_td()}">' + "<br>".join(h(x) for x in s[k]) + "</td>" for k in ("Given", "When", "Then"))
        out.append(f'<tr><td style="{_td()};font-family:Roboto Mono;font-size:10pt;font-weight:bold">{h(ac.key)}</td>{cells}{_ci_cell(ac, r.version)}</tr>')
    out.append("</table>")
    out.append(
        f'<p style="color:{MUTED};font-size:9pt">Name tests <span style="font-family:Roboto Mono">{h(r.acs[0].key if r.acs else r.key)}@v{r.version}</span> … so CI results land here.</p>'
    )
    return out


def _history(releases: list[dict]) -> list[str]:
    out = ["<h1>Change history</h1>"]
    if not releases:
        return out + [f'<p style="color:{MUTED}">No releases yet.</p>']
    out.append(
        '<table style="border-collapse:collapse;width:100%"><tr>'
        f'<th style="{_th("56pt")}">Version</th><th style="{_th("120pt")}">Approved</th><th style="{_th()}">Change</th><th style="{_th("80pt")}">By</th></tr>'
    )
    for rel in reversed(releases):
        out.append(
            f'<tr><td style="{_td()};font-weight:bold">v{h(rel.get("version", ""))}</td><td style="{_td()}">{h(_when(rel.get("at", "")))}</td>'
            f'<td style="{_td()}">{h(rel.get("what", ""))}</td><td style="{_td()}">{h(rel.get("by", ""))}</td></tr>'
        )
    out.append("</table>")
    return out


def render(title: str, reqs: list[Requirement], releases: list[dict], version: str, repo: str = "", now: str = "") -> str:
    """The living product spec: every approved requirement as it reads now, whichever initiative added it."""
    reqs = sorted((r for r in reqs if r.status == "approved"), key=lambda r: int(r.key.split("-")[1]))
    issue = _issue_link(repo)
    out = _head(
        title,
        "Product spec",
        version,
        len(reqs),
        now,
        "Spec Keeper keeps this document: every requirement as it reads today. Each was agreed in a meeting or a Slack thread and approved in Slack.",
    )
    out.append("<h1>Summary</h1>")
    out += _summary(reqs, issue) if reqs else [f'<p style="color:{MUTED}">Nothing approved yet.</p>']
    out.append("<h1>Requirements</h1>")
    for r in reqs:
        out += _requirement(r, issue)
    out += _history(releases)
    out.append("</body></html>")
    return "\n".join(out)


def render_initiative(name: str, parts: dict, releases: list[dict], version: str, repo: str = "", now: str = "") -> str:
    """One initiative's PRD (\"Checkout v2\"): what it adds, what it changes in the current product, what it
    retires, and the current product as context. Built from the same records as the product spec."""
    issue = _issue_link(repo)
    new, changed, retired, context = parts["new"], parts["changed"], parts["retired"], parts["context"]
    out = _head(
        f"{name} · PRD",
        "Product requirements",
        version,
        len(new) + len(changed),
        now,
        f"Spec Keeper keeps this document: what {name} adds and changes. The product spec has every requirement.",
    )
    out.append("<h1>Summary</h1>")
    if new or changed:
        out += _summary([r for _, r in changed] + new, issue)
    else:
        out.append(f'<p style="color:{MUTED}">Nothing approved for {h(name)} yet. Capture its meeting notes in Slack and approve them.</p>')
    out.append("<h1>Changes to current behaviour</h1>")
    if changed:
        for before, r in changed:
            out += _requirement(r, issue, before)
    else:
        out.append(f'<p style="color:{MUTED}">None.</p>')
    out.append("<h1>New requirements</h1>")
    if new:
        for r in new:
            out += _requirement(r, issue)
    else:
        out.append(f'<p style="color:{MUTED}">None yet.</p>')
    if retired:
        out.append("<h1>Retired</h1>")
        out += [f"<p><b>{h(r.key)}</b> · {h(r.title)}</p>" for r in retired]
    out.append("<h1>Current behaviour (context)</h1>")
    if context:
        out.append(f'<p style="color:{MUTED}">Already in the product and unchanged by {h(name)}. Details are in the product spec.</p>')
        out += _summary(context, issue)
    else:
        out.append(f'<p style="color:{MUTED}">Nothing else approved yet.</p>')
    out += _history(releases)
    out.append("</body></html>")
    return "\n".join(out)
