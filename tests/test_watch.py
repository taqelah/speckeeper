"""Thread watch: in threads Spec Keeper was mentioned in, it suggests a capture when the talk changes a
requirement. End to end through the HTTP app with the fake Slack; test ids match the build plan (W-)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_e2e import CHANGE, KICKOFF, App, Asker, Gemini, kickoff_approved, settings

from speckeeper.extract import triage
from speckeeper.main import create_app
from speckeeper.models import Requirement, Source

ROOT = "100.1"  # the kickoff mention starts the thread
CAP = {"type": "message", "channel": "C1", "user": "UWEI", "text": "Storage costs went up, let's cap uploads at 10 MB", "ts": "200.1", "thread_ts": ROOT}


class Triage:
    """Answers triage prompts from a queue; "__new__" becomes the newest message's ts."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else {"verdict": "none", "target": "", "confidence": 0.1, "summary": "", "source_ts": []}
        if isinstance(reply, Exception):
            raise reply
        new = prompt.split("New messages (ts | who | text):", 1)[1].strip().splitlines()
        out = dict(reply)
        out["source_ts"] = [new[-1].split(" | ")[0] if t == "__new__" else t for t in reply["source_ts"]]
        return out


SAYS_CHANGE = {"verdict": "change", "target": "REQ-7", "confidence": 0.93, "summary": "upload limit 25 MB → 10 MB", "source_ts": ["__new__"]}


def watched_app(*triage_replies, gemini=None) -> tuple[App, Triage]:
    app = App(gemini or Gemini(KICKOFF, CHANGE), thread=[{"ts": ROOT, "user": "UMAYA", "text": "kickoff notes"}])
    kickoff_approved(app)  # REQ-7 (25 MB) approved; the mention made this thread a watched one
    t = Triage(*triage_replies)
    app.svc.triage_generate = t
    return app, t


def say(app: App, event: dict) -> None:
    app.slack.thread.append({k: event[k] for k in ("ts", "user", "text") if k in event})
    app.message(event)


def suggestions(app: App) -> list[dict]:
    return [m for m in app.slack.posted("chat.postMessage") if "review_suggestion" in json.dumps(m.get("blocks", []))]


def test_W_01_a_change_in_a_watched_thread_gets_one_suggestion():
    app, t = watched_app(SAYS_CHANGE)
    say(app, CAP)
    (card,) = suggestions(app)
    assert "changes *REQ-7*" in json.dumps(card["blocks"]) and "Wei Tan" in json.dumps(card["blocks"]) and card["thread_ts"] == ROOT
    assert "REQ-7 | Receipt uploads are limited to 25 MB" in t.prompts[0]
    assert "200.1 | UWEI | Storage costs went up" in t.prompts[0].split("New messages (ts | who | text):")[1]


def test_W_02_a_thread_it_was_never_mentioned_in_costs_nothing():
    app, t = watched_app(SAYS_CHANGE)
    say(app, {**CAP, "thread_ts": "999.1"})
    assert t.prompts == [] and suggestions(app) == []


def test_W_03_several_quick_messages_are_triaged_once(monkeypatch):
    s = settings()
    s = type(s)(**{**s.__dict__, "watch_quiet_seconds": 0.3})
    app, t = watched_app(SAYS_CHANGE)
    with TestClient(create_app(s, app.svc, slack_client=app.slack.client(), fetch_doc=None, wait=False)) as http:
        app.http = http
        for i, text in enumerate(["hmm, storage bill is high", "maybe cap uploads?", "ok let's cap uploads at 10 MB"]):
            say(app, {**CAP, "ts": f"20{i}.1", "text": text})
        deadline = time.time() + 5
        while not suggestions(app) and time.time() < deadline:
            time.sleep(0.05)
    assert len(t.prompts) == 1 and len(suggestions(app)) == 1
    assert "ok let's cap uploads" in t.prompts[0] and "hmm, storage bill" in t.prompts[0].split("New messages (ts | who | text):")[1]


@pytest.mark.parametrize(
    "reply",
    [
        {**SAYS_CHANGE, "confidence": 0.5},
        {"verdict": "none", "target": "", "confidence": 0.99, "summary": "chat", "source_ts": ["__new__"]},
    ],
)
def test_W_04_unsure_or_nothing_means_silence(reply):
    app, t = watched_app(reply)
    say(app, CAP)
    assert len(t.prompts) == 1 and suggestions(app) == []


@pytest.mark.parametrize("extra", [{"bot_id": "B9"}, {"subtype": "message_changed"}, {"subtype": "channel_join"}])
def test_W_05_bots_edits_and_joins_are_ignored(extra):
    app, t = watched_app(SAYS_CHANGE)
    say(app, {**CAP, **extra})
    assert t.prompts == []


def test_W_06_mute_stops_suggestions_and_unmute_brings_them_back():
    app, t = watched_app(SAYS_CHANGE)
    app.mention("mute", ts="150.1", thread_ts=ROOT)
    assert "Muted" in app.slack.posted("chat.postMessage")[-1]["text"]
    say(app, CAP)
    assert t.prompts == []
    app.mention("unmute", ts="300.1", thread_ts=ROOT)
    say(app, {**CAP, "ts": "301.1"})
    assert len(t.prompts) == 1 and len(suggestions(app)) == 1
    assert "200.1" not in t.prompts[0].split("New messages (ts | who | text):")[1]  # what was said while muted stays unsuggested


def test_W_07_the_same_change_is_suggested_once_but_a_different_one_is_new():
    to_8 = {**SAYS_CHANGE, "summary": "upload limit 25 MB → 8 MB"}
    app, t = watched_app(SAYS_CHANGE, {**SAYS_CHANGE, "summary": "receipt upload limit 25 MB → 10 MB"}, to_8)
    say(app, CAP)
    say(app, {**CAP, "ts": "201.1", "text": "yes 10 MB, confirmed"})
    assert len(t.prompts) == 2 and len(suggestions(app)) == 1  # reworded, same numbers: not again
    say(app, {**CAP, "ts": "202.1", "text": "actually let's cap at 8 MB"})
    assert len(suggestions(app)) == 2 and "8 MB" in json.dumps(suggestions(app)[-1]["blocks"])


def test_W_08_review_runs_the_normal_capture_and_writes_nothing_before_approval():
    app, _t = watched_app(SAYS_CHANGE)
    say(app, CAP)
    card = suggestions(app)[0]
    value = card["blocks"][1]["elements"][0]["value"]
    app.click("review_suggestion", value, {"ts": "1500.0", "blocks": card["blocks"]})
    reviewing, change = app.slack.posted("chat.update")[-2:]
    assert "is reviewing this" in reviewing["text"] and "review_suggestion" not in json.dumps(reviewing["blocks"])
    assert "Change to REQ-7" in json.dumps(change["blocks"]) and "10 MB" in json.dumps(change["blocks"])
    assert app.svc.store.get_requirement("REQ-7").version == 1 and app.issues.updated == []
    review_prompt = app.svc.generate.prompts[-1].split("Discussion (ts | who | text):")[1]
    assert "200.1" in review_prompt and "kickoff notes" not in review_prompt  # only what the suggestion rests on


def test_W_09_not_a_change_closes_the_card_and_counts_a_false_alarm():
    app, _t = watched_app(SAYS_CHANGE)
    say(app, CAP)
    card = suggestions(app)[0]
    app.click("ignore_suggestion", card["blocks"][1]["elements"][1]["value"], {"ts": "1500.0", "blocks": card["blocks"]})
    assert "marked this as not a change" in app.slack.posted("chat.update")[-1]["text"]
    assert app.svc.store.get_meta("watch_false_alarms") == "1"


def test_W_10_a_gemini_error_means_silence():
    app, t = watched_app(RuntimeError("429 RESOURCE_EXHAUSTED"))
    say(app, CAP)
    assert len(t.prompts) == 1 and suggestions(app) == []


def test_W_11_an_answer_citing_old_messages_is_dropped():
    app, _t = watched_app({**SAYS_CHANGE, "source_ts": [ROOT]})
    say(app, CAP)
    assert suggestions(app) == []


def test_W_12_a_mention_is_left_to_the_mention_handler():
    app, t = watched_app(SAYS_CHANGE)
    say(app, {**CAP, "text": "<@UBOT> what do you think?"})
    assert t.prompts == []


def test_triage_ignores_a_change_to_a_requirement_that_does_not_exist():
    approved = [Requirement(key="REQ-7", title="25 MB", status="approved")]
    new = [Source(ts="2.0", user="U", text="cap at 10 MB")]

    def gen(p, s):
        return {"verdict": "change", "target": "REQ-99", "confidence": 0.99, "summary": "x", "source_ts": ["2.0"]}

    assert triage([], new, gen, approved, 0.8) is None


def reactions(app: App, method: str) -> list[str]:
    return [a["timestamp"] for a in app.slack.posted(method) if a.get("name") == "eyes" and a["timestamp"] != ROOT]


def test_W_13_eyes_while_looking_and_left_on_what_the_suggestion_rests_on():
    app, _t = watched_app({"verdict": "none", "target": "", "confidence": 0.9, "summary": "", "source_ts": []}, SAYS_CHANGE)
    say(app, {**CAP, "ts": "200.1", "text": "lunch?"})
    assert reactions(app, "reactions.add") == ["200.1"] and reactions(app, "reactions.remove") == ["200.1"]
    say(app, {**CAP, "ts": "201.1"})
    assert "201.1" in reactions(app, "reactions.add") and "201.1" not in reactions(app, "reactions.remove")


def test_W_14_eyes_on_a_mention_until_the_reply_is_posted():
    app = App(asker=Asker())
    app.mention("why is the limit 25 MB?", ts="300.1")
    names = [m for m, a in app.slack.calls if m in ("reactions.add", "reactions.remove", "chat.update")]
    assert names == ["reactions.add", "chat.update", "reactions.remove"]


def test_W_15_help_and_mute_get_no_eyes():
    app, _t = watched_app()
    app.mention("hello", ts="300.1", thread_ts=ROOT)
    app.mention("mute", ts="301.1", thread_ts=ROOT)
    assert reactions(app, "reactions.add") == [] or "300.1" not in reactions(app, "reactions.add")
