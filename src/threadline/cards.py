"""Block Kit messages: the approval card and what it becomes after a click."""

from __future__ import annotations

from .models import Proposal, Requirement

APPROVE, EDIT, DISMISS = "approve_proposal", "edit_proposal", "dismiss_proposal"
APPROVE_ALL, REVIEW_EACH, DISMISS_ALL = "approve_group", "review_group", "dismiss_group"


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
    return f"<@{s.user}> {s.permalink and f'<{s.permalink}|msg>' or s.ts}"


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
