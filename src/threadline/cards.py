"""Block Kit messages: the approval card and what it becomes after a click."""

from __future__ import annotations

from .models import Proposal, Requirement

APPROVE, EDIT, DISMISS = "approve_proposal", "edit_proposal", "dismiss_proposal"


def _gherkin(req: Requirement) -> str:
    lines = []
    for i, ac in enumerate(req.acs, 1):
        key = ac.key or f"AC-{i}"
        for j, step in enumerate(ac.gherkin.splitlines()):
            lines.append(f"{key:<8}{step}" if j == 0 else f"{'':<8}{step}")
    return "\n".join(lines)


def _sources(req: Requirement) -> str:
    return "Source: " + ", ".join(f"<@{s.user}> {s.permalink and f'<{s.permalink}|msg>' or s.ts}" for s in req.sources)


def approval_card(p: Proposal) -> list[dict]:
    req = p.requirement
    n = len(req.acs)
    head = f"From this thread I captured 1 requirement and {n} acceptance {'criterion' if n == 1 else 'criteria'}:"
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
