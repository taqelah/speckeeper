"""Block Kit messages: the approval card and what it becomes after a click."""

from __future__ import annotations

import difflib
import json
import re

from .models import Proposal, Requirement
from .prd import meeting_label, source_parts

APPROVE, EDIT, DISMISS = "approve_proposal", "edit_proposal", "dismiss_proposal"
APPROVE_ALL, REVIEW_EACH, DISMISS_ALL = "approve_group", "review_group", "dismiss_group"
REVIEW_SUGGESTION, IGNORE_SUGGESTION = "review_suggestion", "ignore_suggestion"


def _esc(text: str) -> str:
    """Slack's three special characters, so text from Gemini or from people can't post links or pings
    (<!channel>, <@U…>, <url|label>) through a card."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(text: str) -> str:
    """Text for inside <url|label>: escaped, and no | or > to close the link early."""
    return _esc(text).replace("|", "¦")


SPECIAL = re.compile(r"<!(channel|here|everyone|subteam\^[^>|]*)(\|[^>]*)?>")


def defang(text: str) -> str:
    """For replies that are meant to carry Slack links (the agent's answers): keep <url|label> and user
    mentions, but turn @channel, @here, @everyone and group pings into plain text."""
    return SPECIAL.sub(lambda m: "@" + m.group(1).split("^")[0], text)


def suggestion_card(s, channel: str, thread_ts: str) -> list[dict]:
    """Thread watch noticed a change. It only offers a capture; the capture still goes through approval."""
    who = s.sources[0].label or "someone"
    what = f"changes *{s.target}*" if s.kind == "change" else "agrees a new requirement"
    value = json.dumps({"c": channel, "t": thread_ts, "r": s.target, "s": s.summary[:200], "m": [m.ts for m in s.sources][:20]})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":bulb: Looks like this {what}: {_esc(s.summary)} ({_esc(who)})."}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": REVIEW_SUGGESTION,
                    "style": "primary",
                    "value": value,
                    "text": {"type": "plain_text", "text": "Review change" if s.kind == "change" else "Review"},
                },
                {"type": "button", "action_id": IGNORE_SUGGESTION, "value": value, "text": {"type": "plain_text", "text": "Not a change"}},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Nothing changes until someone approves. `@Spec Keeper mute` stops suggestions in this thread."}],
        },
    ]


def _gherkin(req: Requirement) -> str:
    lines = []
    for i, ac in enumerate(req.acs, 1):
        key = ac.key or f"AC-{i}"
        for j, step in enumerate(ac.gherkin.splitlines()):
            lines.append(f"{key:<8}{step}" if j == 0 else f"{'':<8}{step}")
    return _esc("\n".join(lines).replace("```", "ʼʼʼ"))


def _source(s) -> str:
    if s.kind == "meeting":
        return f"<{s.permalink}|{_label(s.label)}>" if s.permalink else _esc(s.label)
    when = s.label.split(", ", 1)[-1] if s.label else s.ts
    user = f"<@{s.user}> " if s.user.isalnum() else ""
    return user + (f"<{s.permalink}|{_label(when)}>" if s.permalink else _esc(when))


def _sources(req: Requirement) -> str:
    """Each meeting once with its times, "Kickoff · Oct 14, 2026 (00:06:45, 00:15:05)", then each message once."""
    meetings = req.model_copy(update={"sources": [s for s in req.sources if s.kind == "meeting"]})
    seen = [(f"<{link}|{_label(title)}>" if link else _esc(title)) + times for title, times, link in source_parts(meetings)]
    for s in req.sources:
        if s.kind != "meeting" and (t := _source(s)) not in seen:
            seen.append(t)
    return "Source: " + ", ".join(seen)


def approval_card(p: Proposal) -> list[dict]:
    req = p.requirement
    n = len(req.acs)
    where = "the meeting notes" if any(s.kind == "meeting" for s in req.sources) else "this thread"
    head = f"From {where} I captured 1 requirement and {n} acceptance {'criterion' if n == 1 else 'criteria'}:"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{req.key or 'REQ (new)'}  {_esc(req.title)}*\n```{_gherkin(req)}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _sources(req)}]},
        {
            "type": "actions",
            "block_id": f"proposal:{p.id}",
            "elements": [
                {"type": "button", "style": "primary", "action_id": APPROVE, "value": p.id, "text": {"type": "plain_text", "text": "Approve"}},
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
        times = sorted({t for s in req.sources if s.kind == "meeting" and (t := meeting_label(s.label)[1])})
        when = f" _({', '.join(times)})_" if times else ""
        lines.append(f"• *{_esc(req.title)}* · {len(req.acs)} {'criterion' if len(req.acs) == 1 else 'criteria'}{when}")
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"From *{_esc(title)}* I captured {len(proposals)} agreed requirement{'' if len(proposals) == 1 else 's'}:"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "_Nothing left to review._"}},
    ]
    if questions:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "Not captured, still open: " + " · ".join(_esc(q) for q in questions)}]})
    blocks.append(
        {
            "type": "actions",
            "block_id": f"group:{group_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": APPROVE_ALL,
                    "value": group_id,
                    "text": {"type": "plain_text", "text": "Approve all as v1.0.0"},
                },
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


REMOVED, ADDED = "#cf222e", "#1a7f37"  # the PRD's failing and passing colours


def _plain_steps(gherkin: str) -> str:
    return " ".join(line.strip() for line in gherkin.splitlines())


def _word_diff(before: str, after: str) -> tuple[list[dict], list[dict]]:
    """Rich-text runs for both sides: words only in the old text struck through, words only in the new
    text bold. Rich text is literal, so text from Gemini or people can't format, link or ping."""
    a, b = re.findall(r"\S+|\s+", before), re.findall(r"\S+|\s+", after)
    old, new = [], []

    def run(out: list[dict], text: str, style: dict | None) -> None:
        if out and out[-1].get("style") == style:
            out[-1]["text"] += text
        else:
            out.append({"type": "text", "text": text, **({"style": style} if style else {})})

    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        for w in a[i1:i2]:
            run(old, w, None if op == "equal" or w.isspace() else {"strike": True})
        for w in b[j1:j2]:
            run(new, w, None if op == "equal" or w.isspace() else {"bold": True})
    return old, new


def _criterion(sign: str, key: str, runs: list[dict]) -> dict:
    head = {"type": "text", "text": f"{sign} {key}", "style": {"code": True}}
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [head, {"type": "text", "text": "  "}, *runs]}]}


def change_card(p: Proposal, old: Requirement | None, decided: str = "") -> dict:
    """A message that changes an approved requirement: what it was, what it would become, the tests
    that would go stale. Approving makes a new version of the same requirement, never a second one.

    Returns chat.postMessage arguments: the heading as blocks, then attachments, because only
    attachments carry a colour bar: the old criteria in red, the new in green, then sources and buttons
    (Slack draws attachments below blocks, so the buttons stay last)."""
    new = p.requirement
    if old is None:
        return {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"This changes {p.target}, which no longer exists."}}], "attachments": []}
    origin = _esc(next((meeting_label(s.label)[0] for s in old.sources if s.kind == "meeting"), ""))
    head = f":warning: *Change to {old.key}* (v{old.version}" + (f", from {origin}" if origin else "") + f"): {_esc(old.title)} → *{_esc(new.title)}*"
    removed, added, changed = [], [], set()
    for i in range(max(len(old.acs), len(new.acs))):
        key = f"AC-{old.key.split('-')[1]}-{i + 1}"
        before = _plain_steps(old.acs[i].gherkin) if i < len(old.acs) else ""
        after = _plain_steps(new.acs[i].gherkin) if i < len(new.acs) else ""
        if before == after:
            continue
        old_runs, new_runs = _word_diff(before, after)
        if before:
            removed.append(_criterion("-", key, old_runs))
            changed.add(key)
        if after:
            added.append(_criterion("+", key, new_runs))
    stale = [a for a in old.acs if a.last_test and a.key in changed]
    if stale:
        removed.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(f":warning: Stale test: `{_esc(a.last_test)}` proves the old text and still passes" for a in stale),
                },
            }
        )
    footer = [
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Was: " + _sources(old).removeprefix("Source: ") + "   ·   Now: " + _sources(new).removeprefix("Source: ")}
            ],
        }
    ]
    if decided:
        footer.append({"type": "context", "elements": [{"type": "mrkdwn", "text": decided}]})
    else:
        footer.append(
            {
                "type": "actions",
                "block_id": f"proposal:{p.id}",
                "elements": [
                    {"type": "button", "style": "primary", "action_id": APPROVE, "value": p.id, "text": {"type": "plain_text", "text": "Approve change"}},
                    {"type": "button", "action_id": DISMISS, "value": p.id, "text": {"type": "plain_text", "text": "Dismiss"}},
                ],
            }
        )
    attachments = []
    if removed:
        attachments.append({"color": REMOVED, "fallback": f"Was: {len(changed)} criteria", "blocks": removed})
    if added:
        attachments.append({"color": ADDED, "fallback": f"Now: {len(added)} criteria", "blocks": added})
    if not removed and not added:
        footer.insert(0, {"type": "section", "text": {"type": "mrkdwn", "text": "_Criteria unchanged._"}})
    attachments.append({"fallback": f"Change to {old.key}", "blocks": footer})
    return {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": head}}], "attachments": attachments}
