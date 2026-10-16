"""ALLOWED_CHANNELS: one store serves every channel, so Spec Keeper works only in the channels it's set up
for. Elsewhere a mention gets a short reply, clicks and thread messages do nothing, and nothing is read."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace

from fastapi.testclient import TestClient
from test_e2e import LINK, App, fetch_ok, kickoff_approved, settings

from speckeeper.main import create_app


def gated(allowed=("proj-checkout",)) -> App:
    app = App()
    app.http = TestClient(create_app(replace(settings(), allowed_channels=allowed), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True))
    return app


def mention_in(app: App, channel: str, text: str, ts: str = "500.1"):
    event = {"type": "app_mention", "user": "UWEI", "text": f"<@UBOT> {text}", "ts": ts, "channel": channel, "event_ts": ts}
    return app.message(event)


def test_a_mention_in_another_channel_is_turned_away_without_reading_anything(capsys):
    app = gated()
    kickoff_approved(app)  # requirements captured in #proj-checkout (C1)
    asked = len(app.svc.engine.generate.prompts) if hasattr(app.svc.engine.generate, "prompts") else 0
    assert mention_in(app, "C2", f"capture {LINK}").status_code == 200
    reply = app.slack.posted("chat.postMessage")[-1]
    assert reply["channel"] == "C2" and "only set up to work in #proj-checkout" in reply["text"]
    assert (len(app.svc.engine.generate.prompts) if hasattr(app.svc.engine.generate, "prompts") else 0) == asked  # no capture
    assert '"channel_refused"' in capsys.readouterr().out


def test_the_allowed_channel_works_as_before():
    app = gated()
    kickoff_approved(app)
    assert [r.key for r in app.svc.store.requirements()] == ["REQ-7", "REQ-8"]


def test_a_click_from_another_channel_changes_nothing():
    app = gated()
    app.mention(f"capture {LINK}")  # a pending card in C1
    card = app.last_card()
    gid = app.button(card, "approve_group")
    payload = {
        "type": "block_actions",
        "user": {"id": "UWEI", "name": "wei"},
        "team": {"id": "T1"},
        "api_app_id": "A1",
        "channel": {"id": "C2"},
        "message": card,
        "container": {"type": "message", "message_ts": card["ts"]},
        "trigger_id": "t",
        "token": "x",
        "actions": [{"action_id": "approve_group", "value": gid, "type": "button", "block_id": "b"}],
    }
    res = app.signed("payload=" + urllib.parse.quote(json.dumps(payload)), "application/x-www-form-urlencoded")
    assert res.status_code == 200 and app.svc.store.requirements() == [] and app.issues_created == []


def test_thread_messages_in_another_channel_are_ignored():
    app = gated()
    before = len(app.slack.calls)
    app.message({"type": "message", "user": "UWEI", "text": "let's cap uploads at 8 MB", "ts": "600.2", "thread_ts": "600.1", "channel": "C2"})
    assert [m for m, _ in app.slack.calls[before:] if m not in ("conversations.info", "auth.test")] == []  # auth.test is Bolt's own


def test_channel_ids_work_too_and_no_list_means_everywhere():
    assert mention_in(gated(allowed=("C2",)), "C2", "hello").status_code == 200
    app = gated(allowed=("C2",))
    mention_in(app, "C2", "hello")
    assert "only set up" not in app.slack.posted("chat.postMessage")[-1]["text"]
    app = gated(allowed=())
    mention_in(app, "C2", "hello")
    assert "only set up" not in app.slack.posted("chat.postMessage")[-1]["text"]
