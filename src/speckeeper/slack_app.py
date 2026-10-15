"""Slack: @mentions, the card buttons and channel messages. Bolt acks every event within Slack's
3 seconds before these run, so slow Gemini calls are fine as long as Cloud Run keeps CPU after the
response (deploy with --no-cpu-throttling)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from slack_bolt.async_app import AsyncApp

from .cards import APPROVE, APPROVE_ALL, DISMISS, DISMISS_ALL, EDIT, REVIEW_EACH, approval_card, baseline_card, change_card, decided_card, decided_group_card
from .models import Source
from .notes import FetchDoc, doc_id_in
from .service import SpecKeeper

log = logging.getLogger("speckeeper")
MENTION = re.compile(r"<@[A-Z0-9]+>")

TZ = "Asia/Singapore"  # how message times are written in citations

# How people ask for a capture, and how questions start. A mention with a notes link always reads the notes.
CAPTURE_WORDS = {"capture", "analyse", "analyze", "review", "check", "read", "sync", "update", "process", "summarise", "summarize"}
QUESTION_WORDS = {"why", "what", "when", "who", "which", "is", "did", "has", "how", "where", "are", "does", "can"}


def _when(iso: str) -> str:
    """'Oct 15, 10:42' in Singapore time, for 'the notes were edited since …'."""
    if not iso:
        return "they were captured"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(TZ)).strftime("%b %d, %H:%M")

HELP = (
    "Mention me in a thread: `@Spec Keeper capture this` to turn what was agreed into a requirement for approval, "
    "share a meeting notes link with me (`@Spec Keeper <link>`) to capture everything the meeting agreed, "
    "or ask me anything about the requirements: `@Spec Keeper why is the upload limit 10 MB?`"
)


def build_slack_app(
    token: str, signing_secret: str, svc: SpecKeeper, fetch_doc: FetchDoc | None = None, client=None, wait: bool = False
) -> AsyncApp:
    """`client` and `wait` are for end-to-end tests: a recording Slack client, and handlers that finish
    before the HTTP response so the test can check what was posted."""
    app = AsyncApp(token=token, signing_secret=signing_secret, client=client, process_before_response=wait)

    @app.event("app_mention")
    async def on_mention(event, client, say):
        text = MENTION.sub("", event.get("text", "")).strip().lower()
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        doc_id = doc_id_in(event.get("text", ""))  # from the raw text: doc ids are case-sensitive
        first = text.split(" ", 1)[0].strip(",.:!") if text else ""
        question = text.endswith("?") or first in QUESTION_WORDS
        if doc_id and not question:
            await capture_meeting(client, channel, thread_ts, doc_id)  # a notes link, whatever the wording
        elif first in CAPTURE_WORDS:
            await capture(client, channel, thread_ts, mention_ts=event["ts"])
        elif svc.asker and question:
            await answer(client, channel, thread_ts, MENTION.sub("", event.get("text", "")).strip(), event.get("user", "slack"))
        else:
            await say(text=HELP, thread_ts=thread_ts)

    async def answer(client, channel: str, thread_ts: str, question: str, user: str) -> None:
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Looking it up…_")
        try:
            reply = await svc.asker.ask(question, user)
        except Exception:
            log.exception("question failed")
            reply = "I couldn't look that up just now. Try again in a minute."
        await client.chat_update(channel=channel, ts=working["ts"], text=reply)

    async def capture(client, channel: str, thread_ts: str, mention_ts: str) -> None:
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Reading the thread…_")
        replies = await client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        raw = [m for m in replies.get("messages", []) if not m.get("bot_id") and m["ts"] != mention_ts]
        names: dict[str, str] = {}
        for uid in {m.get("user", "") for m in raw if m.get("user")}:
            try:
                prof = (await client.users_info(user=uid))["user"]
                names[uid] = prof.get("profile", {}).get("display_name") or prof.get("real_name") or uid
            except Exception:
                names[uid] = uid
        messages = []
        for m in raw:
            try:
                link = (await client.chat_getPermalink(channel=channel, message_ts=m["ts"]))["permalink"]
            except Exception:
                link = ""
            when = datetime.fromtimestamp(float(m["ts"]), ZoneInfo(TZ)).strftime("%b %d %H:%M")
            messages.append(
                Source(ts=m["ts"], user=m.get("user", ""), text=m.get("text", ""), permalink=link, label=f"{names.get(m.get('user', ''), 'someone')}, {when}")
            )
        try:
            proposals, questions = await asyncio.to_thread(svc.capture, channel, thread_ts, messages)
        except Exception:
            log.exception("capture failed")
            await client.chat_update(channel=channel, ts=working["ts"], text="I couldn't read this thread just now. Try again in a minute.")
            return
        if not proposals:
            note = "I didn't find an agreed requirement with a source in this thread."
            if questions:
                note += "\nOpen questions:\n" + "\n".join(f"• {q}" for q in questions)
            await client.chat_update(channel=channel, ts=working["ts"], text=note)
            return
        for i, p in enumerate(proposals):
            blocks = change_card(p, svc.store.get_requirement(p.target)) if p.kind == "change" else approval_card(p)
            if i == 0:
                res = await client.chat_update(channel=channel, ts=working["ts"], text="Captured a requirement", blocks=blocks)
            else:
                res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Captured a requirement", blocks=blocks)
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
                else "I couldn't read those notes just now. Try again in a minute."
            )
            await client.chat_update(channel=channel, ts=working["ts"], text=text)
            return

        if result.status == "unchanged":
            approved = [p.requirement.key for p in result.proposals if p.status == "approved"]
            pending = [p for p in result.proposals if p.status == "pending"]
            if pending:
                # The old card may be lost in the channel or its buttons dead: post a fresh one here.
                note = f"These notes haven't changed since they were captured; {len(pending)} still waiting for approval" + (
                    f" ({len(approved)} already approved: {', '.join(approved)})" if approved else ""
                ) + "."
                await client.chat_update(channel=channel, ts=working["ts"], text=note, blocks=baseline_card(result.group_id, result.title, pending, []))
                for p in pending:
                    svc.set_card(p.id, working["ts"])
                return
            note = f"These notes haven't changed since they were captured: {len(approved)} approved ({', '.join(approved) or 'none'}). The PRD is at v{svc.prd_version or '–'}."
            await client.chat_update(channel=channel, ts=working["ts"], text=note)
            return

        if result.status == "changed":
            since = _when(result.captured_at)
            if not result.proposals:
                await client.chat_update(channel=channel, ts=working["ts"], text=f"The notes were edited since {since}, but nothing new or different was agreed.")
                return
            changes = [p for p in result.proposals if p.kind == "change"]
            new = [p for p in result.proposals if p.kind == "new"]
            parts = ([f"{len(new)} new requirement" + ("s" if len(new) != 1 else "")] if new else []) + [f"a change to {p.target}" for p in changes]
            await client.chat_update(channel=channel, ts=working["ts"], text=f":pencil2: The notes were edited since {since}: " + ", ".join(parts) + ". Cards below.")
            for p in result.proposals:
                blocks = change_card(p, svc.store.get_requirement(p.target)) if p.kind == "change" else approval_card(p)
                res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Change from the meeting notes" if p.kind == "change" else "New requirement from the meeting notes", blocks=blocks)
                svc.set_card(p.id, res["ts"])
            return

        if not result.proposals:
            await client.chat_update(channel=channel, ts=working["ts"], text="I didn't find agreed requirements in those notes.")
            return
        await client.chat_update(
            channel=channel, ts=working["ts"], text=f"Captured {len(result.proposals)} requirements from {result.title}",
            blocks=baseline_card(result.group_id, result.title, result.proposals, result.questions),
        )

    STALE = "This card is from before Spec Keeper restarted, so its requirements are gone. Capture the notes again."

    async def stale(client, body) -> None:
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=STALE, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": STALE}}])

    async def working(client, body, text: str) -> None:
        """Swap a card's buttons for a progress line at once, so a second click can't land while issues are written."""
        kept = [blk for blk in body["message"]["blocks"] if blk["type"] != "actions"]
        kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":hourglass_flowing_sand: {text}"}]})
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=text, blocks=kept)

    def _group_title(body) -> str:
        first = body["message"]["blocks"][0]["text"]["text"]
        return first.split("*")[1] if first.count("*") >= 2 else "the meeting"

    @app.action(APPROVE_ALL)
    async def on_approve_all(ack, body, client):
        await ack()
        gid, user = body["actions"][0]["value"], body["user"]["id"]
        if not svc.store.proposals_in_group(gid):
            await stale(client, body)
            return
        await working(client, body, f"Approving for <@{user}>…")
        done, version = await asyncio.to_thread(svc.approve_group, gid, user, body["user"].get("name", ""))
        if not done:
            return
        keys = ", ".join(p.requirement.key for p in done)
        issues = sum(1 for p in done if p.requirement.issue_number)
        note = f":white_check_mark: Approved by <@{user}> as *PRD v{version}*: {keys}." + (" Written to the PRD." if svc.prd else "") + (f" {issues} issues opened." if issues else "")
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
        gid, user = body["actions"][0]["value"], body["user"]["id"]
        if not svc.store.proposals_in_group(gid):
            await stale(client, body)
            return
        gone = [p for p in (svc.dismiss(p.id, user) for p in svc.store.proposals_in_group(gid)) if p]
        note = f"Dismissed by <@{user}>. Nothing was written."
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_group_card(gid, _group_title(body), gone, note))

    @app.action(APPROVE)
    async def on_approve(ack, body, client):
        await ack()
        pid, user = body["actions"][0]["value"], body["user"]["id"]
        if svc.store.get_proposal(pid) is None:
            await stale(client, body)
            return
        await working(client, body, f"Approving for <@{user}>…")
        p, version = await asyncio.to_thread(svc.approve, pid, user, body["user"].get("name", ""))
        if p is None:
            return
        r = p.requirement
        if p.kind == "change":
            outdated = [a.key for a in r.acs if a.ci_status == "stale"]
            note = f":white_check_mark: Change approved by <@{user}>. *{r.key} is now v{r.version}*, PRD v{version}."
            if r.issue_number:
                note += f" Issue #{r.issue_number} updated."
            if outdated:
                note += f" Stale until tests are updated to @v{r.version}: {', '.join(outdated)}."
            # Keep the diff on the card; swap the buttons for what happened.
            kept = [blk for blk in body["message"]["blocks"] if blk["type"] != "actions"]
            kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
            await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=kept)
            return
        note = f":white_check_mark: Approved by <@{user}> as *{r.key}*. PRD is now v{version}." + (" Added to the PRD." if svc.prd else "") + (f" Opened #{r.issue_number}." if r.issue_number else "")
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_card(p, note))

    @app.action(DISMISS)
    async def on_dismiss(ack, body, client):
        await ack()
        pid, user = body["actions"][0]["value"], body["user"]["id"]
        p = svc.dismiss(pid, user)
        if p is None:
            return
        note = f"Dismissed by <@{user}>. Nothing was written."
        await client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=note, blocks=decided_card(p, note))

    @app.action(EDIT)
    async def on_edit(ack, body, client):
        await ack()
        await client.chat_postEphemeral(
            channel=body["channel"]["id"], user=body["user"]["id"], text="Editing is coming next. For now, dismiss, fix the thread and capture again."
        )

    @app.event("message")
    async def on_message(event):
        # Drift detection hooks in here (Saturday): triage each new human message with Flash.
        return

    return app
