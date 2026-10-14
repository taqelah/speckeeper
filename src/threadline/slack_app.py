"""Slack: @mentions, the card buttons and channel messages. Bolt acks every event within Slack's
3 seconds before these run, so slow Gemini calls are fine as long as Cloud Run keeps CPU after the
response (deploy with --no-cpu-throttling)."""

from __future__ import annotations

import asyncio
import logging
import re

from slack_bolt.async_app import AsyncApp

from .cards import APPROVE, DISMISS, EDIT, approval_card, decided_card
from .models import Source
from .service import Threadline

log = logging.getLogger("threadline")
MENTION = re.compile(r"<@[A-Z0-9]+>")

HELP = "Mention me in a thread: `@Threadline capture this` to turn what was agreed into a requirement for approval."


def build_slack_app(token: str, signing_secret: str, svc: Threadline) -> AsyncApp:
    app = AsyncApp(token=token, signing_secret=signing_secret)

    @app.event("app_mention")
    async def on_mention(event, client, say):
        text = MENTION.sub("", event.get("text", "")).strip().lower()
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        if text.startswith("capture"):
            await capture(client, channel, thread_ts, mention_ts=event["ts"])
        elif text.startswith("why"):
            await say(text="Answering why is coming next: it will cite who decided, when and the thread.", thread_ts=thread_ts)
        else:
            await say(text=HELP, thread_ts=thread_ts)

    async def capture(client, channel: str, thread_ts: str, mention_ts: str) -> None:
        working = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="_Reading the thread…_")
        replies = await client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        messages = [
            Source(ts=m["ts"], user=m.get("user", ""), text=m.get("text", ""))
            for m in replies.get("messages", [])
            if not m.get("bot_id") and m["ts"] != mention_ts
        ]
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
            blocks = approval_card(p)
            if i == 0:
                res = await client.chat_update(channel=channel, ts=working["ts"], text="Captured a requirement", blocks=blocks)
            else:
                res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Captured a requirement", blocks=blocks)
            svc.set_card(p.id, res["ts"])

    @app.action(APPROVE)
    async def on_approve(ack, body, client):
        await ack()
        pid, user = body["actions"][0]["value"], body["user"]["id"]
        p = await asyncio.to_thread(svc.approve, pid, user)
        if p is None:
            return
        where = " and opened #" + str(p.requirement.issue_number) if p.requirement.issue_number else ""
        note = f":white_check_mark: Approved by <@{user}> as *{p.requirement.key}*. Added to the PRD{where}."
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
