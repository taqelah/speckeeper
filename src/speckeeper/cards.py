"""Block Kit messages: the approval card and what it becomes after a click."""

from __future__ import annotations

import json

from .models import Proposal, Requirement

APPROVE, EDIT, DISMISS = "approve_proposal", "edit_proposal", "dismiss_proposal"
APPROVE_ALL, REVIEW_EACH, DISMISS_ALL = "approve_group", "review_group", "dismiss_group"
REVIEW_SUGGESTION, IGNORE_SUGGESTION = "review_suggestion", "ignore_suggestion"


def _esc(text: str) -> str:
    """Slack's three special characters, so model text can't post links or pings."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def suggestion_card(s, channel: str, thread_ts: str) -> list[dict]:
    """Thread watch noticed a change. It only offers a capture; the capture still goes through approval."""
    who = s.sources[0].label or "someone"
    what = f"changes *{s.target}*" if s.kind == "change" else "agrees a new requirement"
    value = json.dumps({"c": channel, "t": thread_ts, "r": s.target, "s": s.summary[:200]})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":bulb: Looks like this {what}: {_esc(s.summary)} ({_esc(who)})."}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": REVIEW_SUGGESTION, "style": "primary", "value": value,
             "text": {"type": "plain_text", "text": "Review change" if s.kind == "change" else "Review"}},
            {"type": "button", "action_id": IGNORE_SUGGESTION, "value": value, "text": {"type": "plain_text", "text": "Not a change"}},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Nothing changes until someone approves. `@Spec Keeper mute` stops suggestions in this thread."}]},
    ]


def _gherkin(req: Requirement) -> str:
    lines = []
    for i, ac in enumerate(req.acs, 1):
        key = ac.key or f"AC-{i}"
        for j, step in enumerate(ac.gherkin.splitlines()):
            lines.append(f"{key:<8}{step}" if j == 0 else f"{'':<8}{step}")
    return "\n".join(lines)


def _source(s) -> str:
    if s.kind == "meeting":
        return f"<{s.permalink}|{s.label}>" if s.permalink else s.label
    when = s.label.split(", ", 1)[-1] if s.label else s.ts
    return f"<@{s.user}> " + (f"<{s.permalink}|{when}>" if s.permalink else when)


def _sources(req: Requirement) -> str:
    seen: list[str] = []
    for s in req.sources:  # notes often repeat a decision; cite each place once
        if (t := _source(s)) not in seen:
            seen.append(t)
    return "Source: " + ", ".join(seen)


def approval_card(p: Proposal) -> list[dict]:
    req = p.requirement
    n = len(req.acs)
    where = "the meeting notes" if any(s.kind == "meeting" for s in req.sources) else "this thread"
    head = f"From {where} I captured 1 requirement and {n} acceptance {'criterion' if n == 1 else 'criteria'}:"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{req.key or 'REQ (new)'}  {req.title}*\n```{_gherkin(req)}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _sources(req)}]},
        {
            "type": "actions",
            "block_id": f"proposal:{p.id}",
            "elements": [
                {"type": "button", "style": "primary", "action_id": APPROVE, "value": p.id, "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": EDIT, "value": p.id, "text": {"type": "plain_text", "text": "Edit"}},
                {"type": "button", "action_id": DISMISS, "value": p.id, "text": {"type": "plain_text", "text": "Dismiss"}},
            ],
        },
    ]


def decided_card(p: Proposal, note: str) -> list[dict]:
    """The card after a click: same content, buttons replaced by who decided."""
    blocks = approval_card(p)[:-1]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks


def baseline_card(group_id: str, title: str, proposals: list[Proposal], questions: list[str]) -> list[dict]:
    """Everything one meeting agreed, approved together as the PRD's first version."""
    lines = []
    for p in proposals:
        req = p.requirement
        times = sorted({s.label.split(", ")[-1] for s in req.sources if s.kind == "meeting" and ", " in s.label})
        when = f" _({', '.join(times)})_" if times else ""
        lines.append(f"• *{req.title}* · {len(req.acs)} {'criterion' if len(req.acs) == 1 else 'criteria'}{when}")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"From *{title}* I captured {len(proposals)} agreed requirements:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "_Nothing left to review._"}},
    ]
    if questions:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Not captured, still open: " + " · ".join(questions)}]})
    blocks.append(
        {
            "type": "actions",
            "block_id": f"group:{group_id}",
            "elements": [
                {"type": "button", "style": "primary", "action_id": APPROVE_ALL, "value": group_id, "text": {"type": "plain_text", "text": "Approve all as v1.0.0"}},
                {"type": "button", "action_id": REVIEW_EACH, "value": group_id, "text": {"type": "plain_text", "text": "Review one by one"}},
                {"type": "button", "action_id": DISMISS_ALL, "value": group_id, "text": {"type": "plain_text", "text": "Dismiss"}},
            ],
        }
    )
    return blocks


def decided_group_card(group_id: str, title: str, proposals: list[Proposal], note: str) -> list[dict]:
    blocks = [b for b in baseline_card(group_id, title, proposals, []) if b["type"] != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return blocks


def _steps(gherkin: str) -> str:
    return " ".join(line.strip() for line in gherkin.splitlines())


def change_card(p: Proposal, old: Requirement | None, decided: str = "") -> list[dict]:
    """A message that changes an approved requirement: what it was, what it would become, the tests
    that would go stale. Approving makes a new version of the same requirement, never a second one."""
    new = p.requirement
    if old is None:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"This changes {p.target}, which no longer exists."}}]
    origin = next((s.label.split(", ")[0] for s in old.sources if s.kind == "meeting"), "")
    head = f":warning: *Change to {old.key}* (v{old.version}" + (f", from {origin}" if origin else "") + f"): {old.title} → *{new.title}*"
    diff = []
    for i in range(max(len(old.acs), len(new.acs))):
        key = f"AC-{old.key.split('-')[1]}-{i + 1}"
        before = _steps(old.acs[i].gherkin) if i < len(old.acs) else ""
        after = _steps(new.acs[i].gherkin) if i < len(new.acs) else ""
        if before == after:
            continue
        if before:
            diff.append(f"- {key}  {before}")
        if after:
            diff.append(f"+ {key}  {after}")
    stale = [a for a in old.acs if a.last_test and any(d.startswith(f"- {a.key} ") for d in diff)]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "```" + ("\n".join(diff) or "(criteria unchanged)") + "```"}},
    ]
    if stale:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(f":warning: Stale test: `{a.last_test}` proves the old text and still passes" for a in stale)}}
        )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Was: " + _sources(old).removeprefix("Source: ") + "   ·   Now: " + _sources(new).removeprefix("Source: ")}]})
    if decided:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": decided}]})
    else:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"proposal:{p.id}",
                "elements": [
                    {"type": "button", "style": "primary", "action_id": APPROVE, "value": p.id, "text": {"type": "plain_text", "text": "Approve change"}},
                    {"type": "button", "action_id": DISMISS, "value": p.id, "text": {"type": "plain_text", "text": "Dismiss"}},
                ],
            }
        )
    return blocks
