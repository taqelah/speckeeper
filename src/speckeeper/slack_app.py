"""Slack: @mentions, the card buttons and channel messages. Bolt acks every event within Slack's
3 seconds before these run, so slow Gemini calls are fine as long as Cloud Run keeps CPU after the
response (deploy with --no-cpu-throttling)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from slack_bolt import BoltResponse
from slack_bolt.async_app import AsyncApp

from .audit import audit
from .budget import BudgetExceeded
from .cards import (
    APPROVE,
    APPROVE_ALL,
    DISMISS,
    DISMISS_ALL,
    EDIT,
    IGNORE_SUGGESTION,
    REVIEW_EACH,
    REVIEW_SUGGESTION,
    _esc,
    approval_card,
    baseline_card,
    change_card,
    decided_card,
    decided_group_card,
    defang,
    suggestion_card,
)
from .models import Source
from .notes import FetchDoc, doc_id_in
from .service import SpecKeeper

log = logging.getLogger("speckeeper")
MENTION = re.compile(r"<@[A-Z0-9]+>")

TZ = "Asia/Singapore"  # how message times are written in citations

# How people ask for a capture, and how questions start. A mention with a notes link always reads the notes.
CAPTURE_WORDS = {"capture", "analyse", "analyze", "review", "check", "read", "sync", "update", "process", "summarise", "summarize"}
RESET_GRACE_SECONDS = 600  # a reset waits this long after someone else last used the demo
QUESTION_WORDS = {"why", "what", "when", "who", "which", "is", "did", "has", "how", "where", "are", "does", "can"}
SMALL_TALK = {"", "help", "hi", "hello", "hey", "thanks", "thank you", "thx", "ok", "okay"}  # a whole mention that gets the help text


def _when(iso: str) -> str:
    """'Oct 15, 10:42' in Singapore time, for 'the notes were edited since …'."""
    if not iso:
        return "they were captured"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(TZ)).strftime("%b %d, %H:%M")


HELP = (
    "Mention me in a thread: `@Spec Keeper capture this` to turn what was agreed into a requirement for approval, "
    "share a meeting notes link with me (`@Spec Keeper <link>`) to capture everything the meeting agreed, "
    "or ask me anything about the requirements: `@Spec Keeper why is the upload limit 10 MB?` "
    "In threads you mention me in, I'll suggest a capture when a requirement changes (`@Spec Keeper mute` stops that)."
)


def build_slack_app(
    token: str,
    signing_secret: str,
    svc: SpecKeeper,
    fetch_doc: FetchDoc | None = None,
    client=None,
    wait: bool = False,
    quiet: float = 15,
    approvers: tuple[str, ...] = (),
    per_user: int = 10,
    per_seconds: float = 600,
    demo_channels: tuple[str, ...] = (),
    allowed_channels: tuple[str, ...] = (),
) -> AsyncApp:
    """`client` and `wait` are for end-to-end tests: a recording Slack client, and handlers that finish
    before the HTTP response so the test can check what was posted (thread watch then checks at once).
    `quiet` is how long a watched thread must be still before thread watch looks at it. `approvers`: Slack
    user ids allowed to approve or dismiss (empty: anyone in the channel). `per_user` requests that call
    Gemini are allowed per user in `per_seconds`. `allowed_channels`: channel names or ids Spec Keeper
    works in (empty: any channel it's in); everything from other channels is turned away."""
    app = AsyncApp(token=token, signing_secret=signing_secret, client=client, process_before_response=wait)
    names: dict[str, str] = {}  # Slack user id -> display name, kept across calls
    pending: dict[str, asyncio.Task] = {}  # thread -> its waiting thread-watch check
    seen: dict[str, list[str]] = {}  # thread -> messages marked 👀 that its next check covers
    recent: dict[str, deque] = {}  # user -> times of their recent Gemini requests
    channel_names: dict[str, tuple[str, float]] = {}  # channel id -> (name, when looked up)

    async def channel_ok(client, channel: str) -> bool:
        """Is this one of the channels Spec Keeper works in? Names are looked up once and kept 10 minutes."""
        if not allowed_channels or not channel or channel in allowed_channels:
            return True
        hit = channel_names.get(channel)
        if hit is None or time.monotonic() - hit[1] > 600:
            try:
                info = await client.conversations_info(channel=channel)
                hit = (info["channel"].get("name", ""), time.monotonic())
            except Exception:  # unknown or inaccessible channel: not allowed
                hit = ("", time.monotonic())
            channel_names[channel] = hit
        return hit[0] in allowed_channels

    @app.middleware
    async def only_allowed_channels(body, client, ack, next):
        # One store serves every channel, so Spec Keeper answers, captures and approves only in the
        # channels it's set up for; anywhere else it would read or change another channel's requirements.
        event = body.get("event") or {}
        channel = event.get("channel") or (body.get("channel") or {}).get("id", "")
        if await channel_ok(client, channel):
            await next()
            return
        if body.get("type") == "block_actions":
            await ack()
            audit("channel_refused", "WARNING", channel=channel, kind="click", user=(body.get("user") or {}).get("id", ""))
        if event.get("type") == "app_mention":  # plain messages elsewhere are ignored without a trace
            audit("channel_refused", "WARNING", channel=channel, kind="mention", user=event.get("user", ""))
            where = ", ".join(f"#{c}" if not c[:1].isupper() else f"<#{c}>" for c in allowed_channels)
            await client.chat_postMessage(channel=channel, thread_ts=event.get("thread_ts") or event.get("ts"), text=f"I'm only set up to work in {where}.")
        return BoltResponse(status=200, body="")  # handled here: a 404 would make Slack retry

    last_use: dict[str, tuple[str, float]] = {}  # channel -> (user, time) of the last mention or click

    def used(channel: str, user: str) -> tuple[str, float] | None:
        """Records who just used Spec Keeper in a channel; returns who did before."""
        before = last_use.get(channel)
        last_use[channel] = (user, time.monotonic())
        return before

    def over_limit(user: str) -> int:
        """Seconds until this user may ask again (0: go ahead). Captures and questions cost Gemini quota."""
        now, q = time.monotonic(), recent.setdefault(user, deque())
        while q and now - q[0] > per_seconds:
            q.popleft()
        if len(q) >= per_user:
            return int(per_seconds - (now - q[0])) + 1
        q.append(now)
        return 0

    async def busy(client, channel: str, thread_ts: str, user: str) -> bool:
        wait_s = over_limit(user)
        if wait_s:
            audit("rate_limited", "WARNING", user=user, channel=channel)
            await client.chat_postEphemeral(
                channel=channel,
                user=user,
                thread_ts=thread_ts,
                text=f"That's {per_user} requests in {int(per_seconds // 60)} minutes. Try again in about {max(1, wait_s // 60)} min.",
            )
        return bool(wait_s)

    async def may_decide(client, body) -> bool:
        """Approve and Dismiss change the PRD: only the approvers may click them, when a list is set."""
        user = body["user"]["id"]
        used(body.get("channel", {}).get("id", ""), user)  # a click counts as using the demo (see reset demo)
        if not approvers or user in approvers:
            return True
        audit("decision_refused", "WARNING", user=user, action=body["actions"][0]["action_id"], channel=body["channel"]["id"])
        await client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user,
            text="Only " + ", ".join(f"<@{u}>" for u in approvers) + " can approve or dismiss requirements here. Ask them to take a look.",
        )
        return False

    def failed(e: Exception, what: str) -> str:
        if isinstance(e, BudgetExceeded):
            return "Today's AI budget is used up, so I'm pausing until tomorrow (UTC). Nothing was lost."
        return f"I couldn't {what} just now. Try again in a minute."

    async def eyes(client, channel: str, ts: str, on: bool = True) -> None:
        """👀 on a message while Spec Keeper works on it, as GitHub apps do. Needs the reactions:write
        scope; without it (or if someone removed the reaction) this quietly does nothing."""
        try:
            if on:
                await client.reactions_add(channel=channel, timestamp=ts, name="eyes")
            else:
                await client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception as e:
            log.info("couldn't %s 👀 on %s: %s", "add" if on else "remove", ts, getattr(e, "response", {}).get("error", e) if hasattr(e, "response") else e)

    @app.event("app_mention")
    async def on_mention(event, client, say):
        text = MENTION.sub("", event.get("text", "")).strip().lower()
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        doc_id = doc_id_in(event.get("text", ""))  # from the raw text: doc ids are case-sensitive
        first = text.split(" ", 1)[0].strip(",.:!") if text else ""
        before = used(channel, event.get("user", ""))
        if text.startswith("reset demo"):
            user = event.get("user", "")
            if channel not in demo_channels:
                await say(text="Reset only works in the demo channel.", thread_ts=thread_ts)
                return
            # Anyone may start clean, but not in the middle of someone else's run.
            if before and before[0] != user and time.monotonic() - before[1] < RESET_GRACE_SECONDS:
                wait_min = int((RESET_GRACE_SECONDS - (time.monotonic() - before[1])) // 60) + 1
                audit("demo_reset_refused", "WARNING", user=user, channel=channel)
                last_use[channel] = before  # a refused reset doesn't take the demo over from the person running it
                await say(
                    text=f"Someone else is using the demo right now. To avoid wiping their run, try `reset demo` again in about {wait_min} min.",
                    thread_ts=thread_ts,
                )
                return
            if await busy(client, channel, thread_ts, user):
                return
            await eyes(client, channel, event["ts"])
            try:
                done = await asyncio.to_thread(svc.reset_demo, user)
                note = (
                    f"Demo reset by <@{user}>: {done['requirements']} requirements cleared, {done['issues']} GitHub issues closed, "
                    "and the PRD is empty again. To start, type `@Spec Keeper` and paste the kickoff notes link."
                )
            except Exception as e:
                log.exception("demo reset failed")
                note = failed(e, "reset the demo")
            finally:
                await eyes(client, channel, event["ts"], on=False)
            await say(text=note, thread_ts=thread_ts)
            return
        if first in ("mute", "unmute"):
            await asyncio.to_thread(svc.mute, channel, thread_ts, first == "mute", event["ts"])
            note = (
                "Muted: I won't suggest captures in this thread. `@Spec Keeper unmute` turns it back on."
                if first == "mute"
                else "Unmuted: I'll suggest a capture here when a requirement changes."
            )
            await say(text=note, thread_ts=thread_ts)
            return
        await asyncio.to_thread(svc.watch, channel, thread_ts, event["ts"])  # thread watch: only threads I'm mentioned in
        question = text.endswith("?") or first in QUESTION_WORDS
        meeting = doc_id and not question  # a notes link, whatever the wording
        # Anything else that isn't small talk is for the agent, commands too: "list all", "print REQ-7".
        ask = svc.asker and text.strip(" .!?") not in SMALL_TALK
        if not meeting and first not in CAPTURE_WORDS and not ask:
            await say(text=HELP, thread_ts=thread_ts)
            return
        if await busy(client, channel, thread_ts, event.get("user", "")):
            return
        await eyes(client, channel, event["ts"])
        try:
            if meeting:
                await capture_meeting(client, channel, thread_ts, doc_id)
            elif first in CAPTURE_WORDS:
                await capture(client, channel, thread_ts, mention_ts=event["ts"])
            else:
                await answer(client, channel, thread_ts, MENTION.sub("", event.get("text", "")).strip(), event.get("user", "slack"))
        finally:
            await eyes(client, channel, event["ts"], on=False)

    async def answer(client, channel: str, thread_ts: str, question: str, user: str) -> None:
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Looking it up…_")
        try:
            reply = await svc.asker.ask(question, user)  # the agent checks the budget before every model call
        except Exception as e:
            log.exception("question failed")
            reply = failed(e, "look that up")
        await client.chat_update(channel=channel, ts=working["ts"], text=defang(reply))

    async def read_thread(client, channel: str, thread_ts: str, skip_ts: str = "", links: bool = True) -> list[Source]:
        """The thread's human messages as citable sources: 'Maya Lim, Oct 15 16:02' with a permalink."""
        replies = await client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        raw = [m for m in replies.get("messages", []) if not m.get("bot_id") and not m.get("subtype") and m["ts"] != skip_ts]
        for uid in {m.get("user", "") for m in raw if m.get("user")} - names.keys():
            try:
                prof = (await client.users_info(user=uid))["user"]
                names[uid] = prof.get("profile", {}).get("display_name") or prof.get("real_name") or uid
            except Exception:
                names[uid] = uid
        messages = []
        for m in raw:
            link = ""
            if links:
                with contextlib.suppress(Exception):
                    link = (await client.chat_getPermalink(channel=channel, message_ts=m["ts"]))["permalink"]
            when = datetime.fromtimestamp(float(m["ts"]), ZoneInfo(TZ)).strftime("%b %d %H:%M")
            messages.append(
                Source(ts=m["ts"], user=m.get("user", ""), text=m.get("text", ""), permalink=link, label=f"{names.get(m.get('user', ''), 'someone')}, {when}")
            )
        return messages

    async def capture(client, channel: str, thread_ts: str, mention_ts: str, only: list[str] | None = None) -> None:
        """`only`: capture just these messages (a thread-watch suggestion's sources), not the whole thread."""
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Reading the thread…_")
        messages = await read_thread(client, channel, thread_ts, skip_ts=mention_ts)
        if only:
            messages = [m for m in messages if m.ts in only] or messages
        try:
            proposals, questions = await asyncio.to_thread(svc.capture, channel, thread_ts, messages)
        except Exception as e:
            log.exception("capture failed")
            await client.chat_update(channel=channel, ts=working["ts"], text=failed(e, "read this thread"))
            return
        if not proposals:
            note = "I didn't find an agreed requirement with a source in this thread."
            if questions:
                note += "\nOpen questions:\n" + "\n".join(f"• {_esc(q)}" for q in questions)  # Gemini text: no pings or links
            await client.chat_update(channel=channel, ts=working["ts"], text=note)
            return
        for i, p in enumerate(proposals):
            card = change_card(p, svc.store.get_requirement(p.target)) if p.kind == "change" else {"blocks": approval_card(p)}
            if i == 0:
                res = await client.chat_update(channel=channel, ts=working["ts"], text="Captured a requirement", **card)
            else:
                res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Captured a requirement", **card)
            svc.set_card(p.id, res["ts"])

    async def capture_meeting(client, channel: str, thread_ts: str, doc_id: str) -> None:
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Reading the meeting notes…_")
        if fetch_doc is None:
            await client.chat_update(channel=channel, ts=working["ts"], text="Reading meeting notes isn't set up on this server yet.")
            return
        try:
            result = await asyncio.to_thread(svc.capture_notes, channel, thread_ts, doc_id, fetch_doc)
        except Exception as e:
            log.exception("meeting capture failed")
            unreadable = type(e).__name__ in ("PermissionError", "HttpError")
            text = (
                "I couldn't open those notes. Share the doc with Spec Keeper's service account (Viewer) and try again."
                if unreadable
                else "Those notes are too long for me to read in one go."
                if type(e).__name__ == "NotesTooLong"
                else failed(e, "read those notes")
            )
            await client.chat_update(channel=channel, ts=working["ts"], text=text)
            return

        if result.status == "unchanged":
            approved = [p.requirement.key for p in result.proposals if p.status == "approved"]
            pending = [p for p in result.proposals if p.status == "pending"]
            if pending:
                # The old card may be lost in the channel or its buttons dead: post a fresh one here.
                note = (
                    f"These notes haven't changed since they were captured; {len(pending)} still waiting for approval"
                    + (f" ({len(approved)} already approved: {', '.join(approved)})" if approved else "")
                    + "."
                )
                await client.chat_update(channel=channel, ts=working["ts"], text=note, blocks=baseline_card(result.group_id, result.title, pending, []))
                for p in pending:
                    svc.set_card(p.id, working["ts"])
                return
            dismissed = sum(1 for p in result.proposals if p.status == "dismissed")
            if not result.proposals:
                note = "These notes haven't changed, and they had no agreed requirements when I read them."
            else:
                note = f"These notes haven't changed since they were captured: {len(approved)} approved ({', '.join(approved) or 'none'})"
                note += f", {dismissed} dismissed." if dismissed else "."
                if svc.prd_version:
                    note += f" The PRD is at {prd_link('v' + svc.prd_version)}."
            await client.chat_update(channel=channel, ts=working["ts"], text=note)
            return

        if result.status == "changed":
            since = _when(result.captured_at)
            if not result.proposals:
                await client.chat_update(
                    channel=channel, ts=working["ts"], text=f"The notes were edited since {since}, but nothing new or different was agreed."
                )
                return
            changes = [p for p in result.proposals if p.kind == "change"]
            new = [p for p in result.proposals if p.kind == "new"]
            parts = ([f"{len(new)} new requirement" + ("s" if len(new) != 1 else "")] if new else []) + [f"a change to {p.target}" for p in changes]
            await client.chat_update(
                channel=channel, ts=working["ts"], text=f":pencil2: The notes were edited since {since}: " + ", ".join(parts) + ". Cards below."
            )
            for p in result.proposals:
                card = change_card(p, svc.store.get_requirement(p.target)) if p.kind == "change" else {"blocks": approval_card(p)}
                res = await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Change from the meeting notes" if p.kind == "change" else "New requirement from the meeting notes",
                    **card,
                )
                svc.set_card(p.id, res["ts"])
            return

        if not result.proposals:
            await client.chat_update(channel=channel, ts=working["ts"], text="I didn't find agreed requirements in those notes.")
            return
        await client.chat_update(
            channel=channel,
            ts=working["ts"],
            text=f"Captured {len(result.proposals)} requirements from {_esc(result.title)}",
            blocks=baseline_card(result.group_id, result.title, result.proposals, result.questions),
        )

    STALE = "This card is from before Spec Keeper restarted, so its requirements are gone. Capture the notes again."

    async def stale(client, body) -> None:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=STALE,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": STALE}}],
            attachments=[],
        )

    def prd_link(text: str = "the PRD") -> str:
        doc_id = getattr(svc.prd, "doc_id", "")
        return f"<https://docs.google.com/document/d/{doc_id}/edit|{text}>" if doc_id else text

    def issue_link(number: int) -> str:
        repo = getattr(svc.issues, "repo", "")
        return f"<https://github.com/{repo}/issues/{number}|#{number}>" if repo else f"#{number}"

    def issues_link(count: int) -> str:
        repo = getattr(svc.issues, "repo", "")
        text = f"{count} issue" + ("s" if count != 1 else "")
        return f"<https://github.com/{repo}/issues?q=is%3Aissue+is%3Aopen+label%3Arequirement|{text}>" if repo else text

    def without_buttons(message: dict, line: str) -> dict:
        """The card as it is, its buttons swapped for a line of text where they were (in the blocks, or in
        the last attachment of a change card). chat.update arguments; attachments are always sent, since
        Slack keeps the old ones, buttons and all, when they are left out."""
        line_block = {"type": "context", "elements": [{"type": "mrkdwn", "text": line}]}
        blocks = [blk for blk in message.get("blocks", []) if blk["type"] != "actions"]
        attachments, placed = [], False
        for att in message.get("attachments", []):
            kept = [blk for blk in att.get("blocks", []) if blk["type"] != "actions"]
            if len(kept) < len(att.get("blocks", [])):
                kept.append(line_block)
                placed = True
            color = att.get("color", "")
            attachments.append({"fallback": att.get("fallback", ""), "blocks": kept, **({"color": "#" + color.lstrip("#")} if color else {})})
        if not placed:
            blocks.append(line_block)
        return {"blocks": blocks, "attachments": attachments}

    async def working(client, body, text: str) -> None:
        """Swap a card's buttons for a progress line at once, so a second click can't land while issues are written."""
        card = without_buttons(body["message"], f":hourglass_flowing_sand: {text}")
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, **card)

    def _group_title(body) -> str:
        first = body["message"]["blocks"][0]["text"]["text"]
        return first.split("*")[1] if first.count("*") >= 2 else "the meeting"

    @app.action(APPROVE_ALL)
    async def on_approve_all(ack, body, client):
        await ack()
        if not await may_decide(client, body):
            return
        gid, user = body["actions"][0]["value"], body["user"]["id"]
        if not svc.store.proposals_in_group(gid):
            await stale(client, body)
            return
        await working(client, body, f"Approving for <@{user}>…")
        done, version = await asyncio.to_thread(svc.approve_group, gid, user, body["user"].get("name", ""))
        if not done:
            return
        keys = ", ".join(p.requirement.key for p in done)
        audit("approved", user=user, requirements=keys, prd_version=version, group=gid)
        issues = sum(1 for p in done if p.requirement.issue_number)
        note = (
            f":white_check_mark: Approved by <@{user}> as *PRD v{version}*: {keys}."
            + (f" Written to {prd_link()}." if svc.prd else "")
            + (f" {issues_link(issues)} opened." if issues else "")
        )
        await client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_group_card(gid, _group_title(body), done, note)
        )

    @app.action(REVIEW_EACH)
    async def on_review_each(ack, body, client):
        await ack()
        gid = body["actions"][0]["value"]
        channel, ts = body["channel"]["id"], body["message"]["ts"]
        if not svc.store.proposals_in_group(gid):
            await stale(client, body)
            return
        pending = [p for p in svc.store.proposals_in_group(gid) if p.status == "pending"]
        for p in pending:
            res = await client.chat_postMessage(channel=channel, thread_ts=p.thread_ts, text="Captured a requirement", blocks=approval_card(p))
            svc.set_card(p.id, res["ts"])
        note = f"Reviewing {len(pending)} requirements one by one, below."
        await client.chat_update(channel=channel, ts=ts, text=note, blocks=decided_group_card(gid, _group_title(body), pending, note))

    @app.action(DISMISS_ALL)
    async def on_dismiss_all(ack, body, client):
        await ack()
        if not await may_decide(client, body):
            return
        gid, user = body["actions"][0]["value"], body["user"]["id"]
        if not svc.store.proposals_in_group(gid):
            await stale(client, body)
            return
        gone = [p for p in (svc.dismiss(p.id, user) for p in svc.store.proposals_in_group(gid)) if p]
        audit("dismissed", user=user, proposals=len(gone), group=gid)
        note = f"Dismissed by <@{user}>. Nothing was written."
        await client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_group_card(gid, _group_title(body), gone, note)
        )

    @app.action(APPROVE)
    async def on_approve(ack, body, client):
        await ack()
        if not await may_decide(client, body):
            return
        pid, user = body["actions"][0]["value"], body["user"]["id"]
        if svc.store.get_proposal(pid) is None:
            await stale(client, body)
            return
        await working(client, body, f"Approving for <@{user}>…")
        p, version = await asyncio.to_thread(svc.approve, pid, user, body["user"].get("name", ""))
        if p is None:
            return
        r = p.requirement
        audit("approved", user=user, requirements=r.key, version=r.version, kind=p.kind, prd_version=version)
        if p.kind == "change":
            outdated = [a.key for a in r.acs if a.ci_status == "stale"]
            note = f":white_check_mark: Change approved by <@{user}>. *{r.key} is now v{r.version}*, PRD v{version}."
            if r.issue_number:
                note += f" Issue {issue_link(r.issue_number)} updated."
            if outdated:
                note += f" Stale until tests are updated to @v{r.version}: {', '.join(outdated)}."
            # Keep the diff on the card; swap the buttons for what happened.
            card = without_buttons(body["message"], note)
            await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, **card)
            return
        note = (
            f":white_check_mark: Approved by <@{user}> as *{r.key}*. PRD is now v{version}."
            + (f" Added to {prd_link()}." if svc.prd else "")
            + (f" Opened {issue_link(r.issue_number)}." if r.issue_number else "")
        )
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_card(p, note))

    @app.action(DISMISS)
    async def on_dismiss(ack, body, client):
        await ack()
        if not await may_decide(client, body):
            return
        pid, user = body["actions"][0]["value"], body["user"]["id"]
        p = svc.dismiss(pid, user)
        if p is None:
            return
        audit("dismissed", user=user, proposal=pid, target=p.target)
        note = f"Dismissed by <@{user}>. Nothing was written."
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_card(p, note), attachments=[])

    @app.action(EDIT)
    async def on_edit(ack, body, client):
        await ack()
        await client.chat_postEphemeral(
            channel=body["channel"]["id"], user=body["user"]["id"], text="Editing is coming next. For now, dismiss, fix the thread and capture again."
        )

    # --- thread watch ---

    async def check_thread(client, channel: str, thread_ts: str) -> None:
        marked = seen.pop(f"{channel}:{thread_ts}", [])
        found = None
        try:
            messages = await read_thread(client, channel, thread_ts, links=False)
            found = await asyncio.to_thread(svc.triage_thread, channel, thread_ts, messages)
        except Exception:
            log.exception("thread watch failed")  # a missed suggestion is fine; a noisy error is not
        keep = {m.ts for m in found.sources} if found else set()
        for ts in marked:  # 👀 stays only on what the suggestion rests on
            if ts not in keep:
                await eyes(client, channel, ts, on=False)
        if found:
            what = f"changes {found.target}" if found.kind == "change" else "agrees a new requirement"
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=f"Looks like this {what}: {_esc(found.summary)}", blocks=suggestion_card(found, channel, thread_ts)
            )

    async def check_later(client, channel: str, thread_ts: str) -> None:
        await asyncio.sleep(quiet)
        pending.pop(f"{channel}:{thread_ts}", None)  # from here on a new message waits for its own check
        await check_thread(client, channel, thread_ts)

    @app.event("message")
    async def on_message(event, client, context):
        thread_ts = event.get("thread_ts")
        if event.get("subtype") or event.get("bot_id") or not thread_ts or thread_ts == event.get("ts"):
            return  # edits, joins, bots, and messages outside a thread
        if context.bot_user_id and f"<@{context.bot_user_id}>" in event.get("text", ""):
            return  # a mention: on_mention has it
        channel = event["channel"]
        state = await asyncio.to_thread(svc.watch_state, channel, thread_ts)
        if not state or state.get("muted"):
            return  # not a thread I was mentioned in: no Gemini call at all
        await eyes(client, channel, event["ts"])
        seen.setdefault(f"{channel}:{thread_ts}", []).append(event["ts"])
        if wait or quiet <= 0:
            await check_thread(client, channel, thread_ts)
            return
        key = f"{channel}:{thread_ts}"
        if key in pending:
            pending.pop(key).cancel()  # still talking: wait for a quiet spell, then look at everything new once
        pending[key] = asyncio.create_task(check_later(client, channel, thread_ts))

    @app.action(REVIEW_SUGGESTION)
    async def on_review_suggestion(ack, body, client):
        await ack()
        v, user = json.loads(body["actions"][0]["value"]), body["user"]["id"]
        if await busy(client, v["c"], v["t"], user):
            return
        note = f"<@{user}> is reviewing this: cards below."
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=note,
            blocks=[body["message"]["blocks"][0], {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}],
        )
        await capture(client, v["c"], v["t"], mention_ts="", only=v.get("m"))

    @app.action(IGNORE_SUGGESTION)
    async def on_ignore_suggestion(ack, body, client):
        await ack()
        v, user = json.loads(body["actions"][0]["value"]), body["user"]["id"]
        await asyncio.to_thread(svc.not_a_change, v["c"], v["t"], user, v["s"])
        note = f"<@{user}> marked this as not a change. I'll keep listening."
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=note,
            blocks=[body["message"]["blocks"][0], {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}],
        )

    return app
