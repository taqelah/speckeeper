"""End to end through the HTTP app, the way Slack and GitHub Actions call it: signed Slack events and
button clicks, and CI webhooks. A recording Slack client stands in for Slack, fakes stand in for
Gemini, the notes doc, GitHub and the PRD, so nothing touches the network. Test ids match the test
plan in the build plan doc (E2E-, N-, C-)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fakes import FakeEngine
from fastapi.testclient import TestClient
from slack_sdk.web.async_client import AsyncWebClient
from test_meeting import DOC, NOTES, FakeIssues, FakePRD

from speckeeper.config import Settings
from speckeeper.main import create_app
from speckeeper.service import SpecKeeper
from speckeeper.store import MemoryStore

SECRET = "signing-secret"
CI = "ci-secret"
LINK = f"<https://docs.google.com/document/d/{DOC}/edit?usp=sharing|kickoff notes>"


class FakeSlack:
    """A local HTTP server that answers Slack's Web API like Slack would and records every call.
    Spec Keeper talks to it over real HTTP, exactly as it talks to slack.com."""

    def __init__(self, thread=None):
        self.calls: list[tuple[str, dict]] = []
        self.thread = thread or []
        self._ts = 1000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                url = urllib.parse.urlparse(self.path)
                method = url.path.rsplit("/", 1)[-1]
                args = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if raw:
                    if "json" in (self.headers.get("Content-Type") or ""):
                        args.update(json.loads(raw))
                    else:
                        args.update({k: v[0] for k, v in urllib.parse.parse_qs(raw).items()})
                for k in ("blocks",):
                    if isinstance(args.get(k), str):
                        args[k] = json.loads(args[k])
                body = fake.respond(method, args)
                out = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/api/"

    def respond(self, method: str, args: dict) -> dict:
        self.calls.append((method, args))
        self._ts += 1
        body = {"ok": True}
        if method == "auth.test":
            body.update(user_id="UBOT", bot_id="BBOT", team_id="T1", user="speckeeper")
        elif method == "chat.postMessage":
            body.update(ts=f"{self._ts}.0", channel=args.get("channel"))
        elif method == "conversations.replies":
            body.update(messages=self.thread)
        elif method == "users.info":
            name = {"UMAYA": "Maya Lim", "UWEI": "Wei Tan"}.get(args.get("user"), "Someone")
            body.update(user={"id": args.get("user"), "real_name": name, "profile": {}})
        elif method == "conversations.info":
            body.update(channel={"id": args.get("channel"), "name": {"C1": "proj-checkout", "C2": "random"}.get(args.get("channel"), "")})
        elif method == "chat.getPermalink":
            body.update(permalink=f"https://slack.test/p{args.get('message_ts')}")
        return body

    def posted(self, method: str) -> list[dict]:
        return [args for m, args in self.calls if m == method]

    def client(self) -> AsyncWebClient:
        return AsyncWebClient(token="xoxb-test", base_url=self.base_url)


def settings() -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_signing_secret=SECRET,
        gcp_project="p",
        gcp_location="l",
        gemini_location="global",
        pro_model="m",
        flash_model="m",
        prd_doc_id="",
        github_token="",
        github_repo="",
        ci_secret=CI,
        store="memory",
    )


KICKOFF = {
    "requirements": [
        {
            "title": "Receipt uploads are limited to 25 MB",
            "acceptance_criteria": [{"given": "a 30 MB file", "when": "uploaded", "then": "show 'File exceeds 25 MB'"}],
            "source_ts": ["__limit__"],
        },
        {
            "title": "Every upload is virus-scanned before it is stored",
            "acceptance_criteria": [{"given": "an infected file", "when": "uploaded", "then": "it is rejected"}],
            "source_ts": ["__scan__"],
        },
    ],
    "open_questions": ["Should photos be compressed on the phone?"],
}
CHANGE = {
    "requirements": [
        {
            "title": "Receipt uploads are limited to 10 MB",
            "acceptance_criteria": [{"given": "a 12 MB file", "when": "uploaded", "then": "show 'File exceeds 10 MB'"}],
            "source_ts": ["200.1"],
            "changes": "REQ-7",
        }
    ],
    "open_questions": [],
}


class Gemini:
    """Answers capture prompts from a queue of replies, filling in the real passage ids."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt, schema):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        ids = {line.split(" | ")[2]: line.split(" | ")[0] for line in prompt.splitlines() if " | notes | " in line}
        limit = [ts for text, ts in ids.items() if "upload size limit" in text]
        scan = [ts for text, ts in ids.items() if text.startswith("Virus scan")]
        out = json.loads(json.dumps(reply))
        for r in out["requirements"]:
            r["source_ts"] = sum(([*limit] if t == "__limit__" else [*scan] if t == "__scan__" else [t] for t in r["source_ts"]), [])
        return out


class Asker:
    def __init__(self, reply="REQ-7 is limited to 25 MB, decided at the kickoff (00:06:45)."):
        self.reply, self.questions = reply, []

    async def ask(self, question, user="slack"):
        self.questions.append(question)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def fetch_ok(doc_id):
    assert doc_id == DOC
    return "Checkout v2 kickoff", NOTES


def fetch_denied(doc_id):
    raise PermissionError("not shared")


class App:
    def __init__(self, gemini=None, asker=None, fetch=fetch_ok, thread=None):
        self.prd, self.issues = FakePRD(), FakeIssues()
        self.issues_created = self.issues.created
        self.svc = SpecKeeper(MemoryStore(first_req=7), FakeEngine(gemini or Gemini(KICKOFF)), self.prd, self.issues, asker)
        self.slack = FakeSlack(thread)
        self.http = TestClient(create_app(settings(), self.svc, slack_client=self.slack.client(), fetch_doc=fetch, wait=True))
        self._event = 0

    def signed(self, body: str, content_type: str, at: int | None = None, secret: str = SECRET):
        ts = at or int(time.time())
        sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        return self.http.post(
            "/slack/events", content=body, headers={"Content-Type": content_type, "X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}
        )

    def mention(self, text: str, ts="100.1", thread_ts=None, **kw):
        self._event += 1
        event = {"type": "app_mention", "user": "UMAYA", "text": f"<@UBOT> {text}", "ts": ts, "channel": "C1", "event_ts": ts}
        if thread_ts:
            event["thread_ts"] = thread_ts
        body = json.dumps({"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": event, "event_id": f"Ev{self._event}", "event_time": 1})
        return self.signed(body, "application/json", **kw)

    def message(self, event: dict):
        self._event += 1
        body = json.dumps({"type": "event_callback", "team_id": "T1", "api_app_id": "A1", "event": event, "event_id": f"Ev{self._event}", "event_time": 1})
        return self.signed(body, "application/json")

    def click(self, action_id: str, value: str, message: dict):
        payload = {
            "type": "block_actions",
            "user": {"id": "UMAYA", "name": "maya"},
            "team": {"id": "T1"},
            "api_app_id": "A1",
            "channel": {"id": "C1"},
            "message": message,
            "container": {"type": "message", "message_ts": message["ts"]},
            "trigger_id": "t",
            "token": "x",
            "actions": [{"action_id": action_id, "value": value, "type": "button", "block_id": "b"}],
        }
        return self.signed("payload=" + urllib.parse.quote(json.dumps(payload)), "application/x-www-form-urlencoded")

    def last_card(self) -> dict:
        update = self.slack.posted("chat.update")[-1]
        return {"ts": update["ts"], "blocks": update.get("blocks", []), "attachments": update.get("attachments", [])}

    def button(self, card: dict, action_id: str) -> str:
        for b in card["blocks"] + [b for a in card.get("attachments", []) for b in a.get("blocks", [])]:
            for e in b.get("elements", []):
                if e.get("action_id") == action_id:
                    return e["value"]
        raise AssertionError(f"no {action_id} button on the card")

    def ci(self, xml: str, secret: str | None = CI):
        headers = {"X-SpecKeeper-Secret": secret} if secret is not None else {}
        return self.http.post("/ci", json={"run_id": "#1", "channel": "C1", "junit_xml": xml}, headers=headers)


def junit(*cases: tuple[str, bool]) -> str:
    rows = "".join(f'<testcase name="{n}"/>' if ok else f'<testcase name="{n}"><failure message="x"/></testcase>' for n, ok in cases)
    return f"<testsuites><testsuite>{rows}</testsuite></testsuites>"


def kickoff_approved(app: App) -> None:
    app.mention(f"capture {LINK}")
    card = app.last_card()
    app.click("approve_group", app.button(card, "approve_group"), card)


# --- the happy path -----------------------------------------------------------------------------


def test_E2E_01_unknown_mention_gets_help_in_the_thread():
    app = App()
    assert app.mention("hello").status_code == 200
    (reply,) = app.slack.posted("chat.postMessage")
    assert "capture this" in reply["text"] and reply["thread_ts"] == "100.1"


def test_E2E_02_meeting_notes_become_one_baseline_card():
    app = App()
    app.mention(f"capture {LINK}")
    assert "Reading the meeting notes" in app.slack.posted("chat.postMessage")[0]["text"]
    card = json.dumps(app.last_card()["blocks"])
    assert "captured 2 agreed requirements" in card and "00:06:45" in card
    assert "Should photos be compressed" in card  # open question shown, not captured
    assert "Approve all as v1.0.0" in card


def test_E2E_03_approve_all_writes_the_baseline_everywhere():
    app = App()
    kickoff_approved(app)
    assert "PRD v1.0.0" in app.slack.posted("chat.update")[-1]["text"]
    assert [r.key for r in app.svc.store.requirements()] == ["REQ-7", "REQ-8"]
    assert app.issues_created == ["REQ-7", "REQ-8"] and app.prd.appended == ["REQ-7", "REQ-8"]
    assert app.prd.notes[-1].startswith("v1.0.0")


def test_E2E_04_double_click_on_approve_all_files_nothing_twice():
    app = App()
    app.mention(f"capture {LINK}")
    card = app.last_card()
    gid = app.button(card, "approve_group")
    app.click("approve_group", gid, card)
    app.click("approve_group", gid, card)
    assert app.issues_created == ["REQ-7", "REQ-8"]


def test_E2E_05_review_one_by_one_posts_a_card_per_requirement():
    app = App()
    app.mention(f"capture {LINK}")
    card = app.last_card()
    before = len(app.slack.posted("chat.postMessage"))
    app.click("review_group", app.button(card, "review_group"), card)
    cards = app.slack.posted("chat.postMessage")[before:]
    assert len(cards) == 2 and all("approve_proposal" in json.dumps(c["blocks"]) for c in cards)


def test_E2E_06_capturing_the_same_notes_again_files_nothing_new():
    gemini = Gemini(KICKOFF)
    app = App(gemini)
    kickoff_approved(app)
    app.mention(f"capture {LINK}", ts="101.1")
    assert "haven't changed since they were captured: 2 approved (REQ-7, REQ-8)" in app.slack.posted("chat.update")[-1]["text"]
    assert len(app.svc.store.requirements()) == 2
    assert len(gemini.prompts) == 1  # unchanged notes: no second Gemini call


def test_E2E_07_a_thread_that_changes_REQ_7_becomes_v2_not_a_new_requirement():
    thread = [{"ts": "200.1", "user": "UWEI", "text": "Storage bill doubled. Let's cap uploads at 10 MB."}, {"ts": "200.2", "user": "UMAYA", "text": "Agreed."}]
    app = App(Gemini(KICKOFF, CHANGE), thread=thread)
    kickoff_approved(app)
    app.mention("capture this", ts="200.3", thread_ts="200.1")
    card = app.last_card()
    text = json.dumps(card)
    assert "Change to REQ-7" in text and "- AC-7-1" in text and "+ AC-7-1" in text
    assert "<@UWEI>" in text and "https://slack.test/p200.1" in text  # cited as a mention (Slack shows the name) with a link
    app.click("approve_proposal", app.button(card, "approve_proposal"), card)
    req7 = app.svc.store.get_requirement("REQ-7")
    assert req7.version == 2 and "10 MB" in req7.title
    assert app.svc.store.get_requirement("REQ-9") is None
    assert "REQ-7 is now v2" in app.slack.posted("chat.update")[-1]["text"]
    assert app.issues.updated[-1][:2] == (1, 2)


def test_E2E_08_green_ci_verifies_a_requirement_in_slack_and_on_its_issue():
    app = App()
    kickoff_approved(app)
    res = app.ci(junit(("AC-7-1@v1 rejects a 30 MB file", True), ("renders the page", True)))
    assert res.json() == {"requirements": ["REQ-7"], "unmapped": ["renders the page"], "posted": True}
    assert "REQ-7 is verified" in app.slack.posted("chat.postMessage")[-1]["text"]
    assert app.issues.comments[-1][0] == 1


def test_E2E_09_after_a_change_the_old_passing_test_is_stale_not_verified():
    thread = [{"ts": "200.1", "user": "UWEI", "text": "Cap uploads at 10 MB."}]
    app = App(Gemini(KICKOFF, CHANGE), thread=thread)
    kickoff_approved(app)
    app.ci(junit(("AC-7-1@v1 rejects a 30 MB file", True)))
    app.mention("capture this", ts="200.3", thread_ts="200.1")
    card = app.last_card()
    assert "Stale test" in json.dumps(card)
    app.click("approve_proposal", app.button(card, "approve_proposal"), card)
    app.ci(junit(("AC-7-1@v1 rejects a 30 MB file", True)))
    last = app.slack.posted("chat.postMessage")[-1]["text"]
    assert "stale" in last and "not verified" in last


def test_E2E_10_questions_go_to_the_agent_and_the_answer_is_posted():
    asker = Asker()
    app = App(asker=asker)
    app.mention("why is the upload limit 25 MB?")
    assert asker.questions == ["why is the upload limit 25 MB?"]
    assert "decided at the kickoff" in app.slack.posted("chat.update")[-1]["text"]


def test_E2E_11_commands_go_to_the_agent_too():
    asker = Asker()
    app = App(asker=asker)
    app.mention("Print REQ-7", ts="300.1")
    app.mention("list all", ts="301.1")
    assert asker.questions == ["Print REQ-7", "list all"]
    assert not any("capture this" in m["text"] for m in app.slack.posted("chat.postMessage"))


# --- negative cases -----------------------------------------------------------------------------


def test_N_01_a_request_with_a_bad_signature_is_rejected_and_nothing_is_posted():
    app = App()
    assert app.mention("capture this", secret="wrong").status_code == 401
    assert app.slack.posted("chat.postMessage") == []


def test_N_02_a_replayed_request_older_than_five_minutes_is_rejected():
    app = App()
    assert app.mention("hello", at=int(time.time()) - 600).status_code == 401
    assert app.slack.posted("chat.postMessage") == []


def test_N_03_ci_without_or_with_a_wrong_secret_is_rejected():
    app = App()
    assert app.ci(junit(("AC-7-1@v1 x", True)), secret="nope").status_code == 401
    assert app.ci(junit(("AC-7-1@v1 x", True)), secret=None).status_code == 401
    assert app.slack.posted("chat.postMessage") == []


def test_N_04_ci_with_broken_xml_is_a_400_not_a_crash():
    app = App()
    assert app.ci("<testsuites><testcase").status_code == 400


def test_N_05_notes_that_cannot_be_opened_get_a_clear_message():
    app = App(fetch=fetch_denied)
    app.mention(f"capture {LINK}")
    assert "couldn't open those notes" in app.slack.posted("chat.update")[-1]["text"]


def test_N_06_a_gemini_failure_during_capture_gets_a_clear_message():
    app = App(Gemini(RuntimeError("503 from Gemini")), thread=[{"ts": "1.1", "user": "UMAYA", "text": "x"}])
    app.mention("capture this", ts="1.2", thread_ts="1.1")
    assert "couldn't read this thread" in app.slack.posted("chat.update")[-1]["text"]


def test_N_07_uncited_requirements_are_dropped_and_said_so():
    invented = {
        "requirements": [{"title": "Invented", "acceptance_criteria": [{"given": "a", "when": "b", "then": "c"}], "source_ts": ["9.9"]}],
        "open_questions": [],
    }
    app = App(Gemini(invented), thread=[{"ts": "1.1", "user": "UMAYA", "text": "Nice weather."}])
    app.mention("capture this", ts="1.2", thread_ts="1.1")
    assert "didn't find an agreed requirement" in app.slack.posted("chat.update")[-1]["text"]
    assert app.svc.store.requirements() == []


def test_N_08_a_click_on_a_card_from_before_a_restart_says_so():
    app = App()
    card = {"ts": "5.5", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "From *Old notes* I captured 2"}}]}
    app.click("approve_group", "g-unknown", card)
    assert "before Spec Keeper restarted" in app.slack.posted("chat.update")[-1]["text"]
    assert app.issues_created == []


def test_N_09_a_rate_limited_question_gets_a_clear_message():
    app = App(asker=Asker(RuntimeError("429 RESOURCE_EXHAUSTED")))
    app.mention("is REQ-7 verified?")
    assert "couldn't look that up" in app.slack.posted("chat.update")[-1]["text"]


def test_N_10_messages_from_bots_are_ignored():
    app = App()
    app.message({"type": "message", "subtype": "bot_message", "bot_id": "BOTHER", "text": "Cap uploads at 10 MB", "ts": "9.1", "channel": "C1"})
    assert app.slack.posted("chat.postMessage") == [] and app.slack.posted("chat.update") == []


def test_N_11_dismiss_writes_nothing():
    app = App()
    app.mention(f"capture {LINK}")
    card = app.last_card()
    app.click("dismiss_group", app.button(card, "dismiss_group"), card)
    assert "Nothing was written" in app.slack.posted("chat.update")[-1]["text"]
    assert app.issues_created == [] and app.prd.appended == [] and app.svc.prd_version == ""


# --- corner cases -------------------------------------------------------------------------------


def test_C_01_a_mention_inside_a_thread_reads_and_replies_in_that_thread():
    thread = [{"ts": "300.1", "user": "UMAYA", "text": "Uploads up to 25 MB."}]
    app = App(Gemini({"requirements": [], "open_questions": []}), thread=thread)
    app.mention("capture this", ts="300.5", thread_ts="300.1")
    assert app.slack.posted("conversations.replies")[0]["ts"] == "300.1"
    assert app.slack.posted("chat.postMessage")[0]["thread_ts"] == "300.1"


def test_C_02_the_capture_mention_itself_is_not_read_as_part_of_the_thread():
    thread = [{"ts": "300.1", "user": "UMAYA", "text": "Uploads up to 25 MB."}, {"ts": "300.5", "user": "UMAYA", "text": "<@UBOT> capture this"}]
    gemini = Gemini({"requirements": [], "open_questions": []})
    app = App(gemini, thread=thread)
    app.mention("capture this", ts="300.5", thread_ts="300.1")
    assert "capture this" not in gemini.prompts[0]


@pytest.mark.parametrize(
    "link",
    [
        f"<https://docs.google.com/document/d/{DOC}/edit>",
        f"<https://docs.google.com/document/d/{DOC}/edit?usp=sharing|notes>",
        f"<https://docs.google.com/document/d/{DOC}/view?tab=t.0>",
    ],
)
def test_C_03_meeting_note_links_in_any_common_form_are_recognised(link):
    app = App()
    app.mention(f"capture {link}")
    assert "Reading the meeting notes" in app.slack.posted("chat.postMessage")[0]["text"]


def test_C_04_a_change_naming_a_requirement_that_does_not_exist_is_treated_as_new():
    wrong = {"requirements": [{**CHANGE["requirements"][0], "changes": "REQ-99"}], "open_questions": []}
    app = App(Gemini(KICKOFF, wrong), thread=[{"ts": "200.1", "user": "UWEI", "text": "Cap uploads at 10 MB."}])
    kickoff_approved(app)
    app.mention("capture this", ts="200.3", thread_ts="200.1")
    assert "approve_proposal" in json.dumps(app.last_card()["blocks"]) and "Change to" not in json.dumps(app.last_card()["blocks"])


def test_C_05_a_failing_test_marks_its_criterion_failing_and_the_requirement_unverified():
    app = App()
    kickoff_approved(app)
    app.ci(junit(("AC-7-1@v1 rejects a 30 MB file", False)))
    last = app.slack.posted("chat.postMessage")[-1]["text"]
    assert "AC-7-1 failing" in last and "not verified" in last


def test_C_06_a_ci_run_that_maps_to_no_requirement_posts_nothing():
    app = App()
    kickoff_approved(app)
    before = len(app.slack.posted("chat.postMessage"))
    res = app.ci(junit(("AC-99-1@v1 unknown", True), ("smoke", True)))
    assert res.json()["requirements"] == [] and len(app.slack.posted("chat.postMessage")) == before


def test_C_07_unicode_and_emoji_in_messages_survive_capture():
    thread = [{"ts": "400.1", "user": "UMAYA", "text": "Reçus jusqu'à 25 Mo 🚀 — d'accord?"}]
    gemini = Gemini({"requirements": [], "open_questions": []})
    app = App(gemini, thread=thread)
    app.mention("capture this", ts="400.2", thread_ts="400.1")
    assert "Reçus jusqu'à 25 Mo 🚀" in gemini.prompts[0]


def test_C_08_capturing_notes_again_while_requirements_wait_reposts_a_working_card():
    app = App(Gemini(KICKOFF))
    app.mention(f"capture {LINK}")
    app.mention(f"capture {LINK}", ts="102.1")  # the first card is lost or its buttons dead
    card = app.last_card()
    assert "haven't changed since they were captured; 2 still waiting" in app.slack.posted("chat.update")[-1]["text"]
    app.click("approve_group", app.button(card, "approve_group"), card)
    assert app.issues_created == ["REQ-7", "REQ-8"]  # approved once, from the fresh card


class EditableNotes:
    """A notes doc someone edits between captures."""

    def __init__(self, text=NOTES):
        self.text = text

    def __call__(self, doc_id):
        return "Checkout v2 kickoff", self.text


def test_C_09_edited_notes_that_change_a_limit_give_a_change_card():
    notes = EditableNotes()
    change = {
        "requirements": [
            {
                "title": "Receipt uploads are limited to 20 MB",
                "acceptance_criteria": [{"given": "a 25 MB file", "when": "uploaded", "then": "show 'File exceeds 20 MB'"}],
                "source_ts": ["__limit__"],
                "changes": "REQ-7",
            }
        ],
        "open_questions": [],
    }
    gemini = Gemini(KICKOFF, change)
    app = App(gemini, fetch=notes)
    kickoff_approved(app)
    notes.text = NOTES.replace("25 MB", "20 MB")
    app.mention(f"capture {LINK}", ts="101.1")
    assert "REQ-7 | Receipt uploads are limited to 25 MB" in gemini.prompts[1]  # read against what's approved
    summary = [u["text"] for u in app.slack.posted("chat.update")][-1]
    assert "The notes were edited since" in summary and "a change to REQ-7" in summary
    card = json.dumps(app.slack.posted("chat.postMessage")[-1]["blocks"])
    assert "Change to REQ-7" in card and "20 MB" in card


def test_C_10_edited_notes_with_a_new_decision_give_one_new_card():
    notes = EditableNotes()
    added = {
        "requirements": [
            {
                "title": "Receipts can be uploaded as HEIC photos",
                "acceptance_criteria": [{"given": "a HEIC photo", "when": "uploaded", "then": "it is accepted"}],
                "source_ts": ["__scan__"],
            }
        ],
        "open_questions": [],
    }
    app = App(Gemini(KICKOFF, added), fetch=notes)
    kickoff_approved(app)
    notes.text = NOTES + "* HEIC photos: agreed to accept HEIC receipts. (00:41:00)\n"
    before = len(app.slack.posted("chat.postMessage"))
    app.mention(f"capture {LINK}", ts="101.1")
    assert "1 new requirement" in app.slack.posted("chat.update")[-1]["text"]
    cards = app.slack.posted("chat.postMessage")[before + 1 :]
    assert len(cards) == 1 and "HEIC" in json.dumps(cards[0]["blocks"])


def test_C_11_edited_notes_where_gemini_repeats_an_approved_requirement_file_nothing():
    notes = EditableNotes()
    app = App(Gemini(KICKOFF, KICKOFF), fetch=notes)  # second reply repeats the kickoff as if new
    kickoff_approved(app)
    notes.text = NOTES + "* Typo fixes only.\n"
    app.mention(f"capture {LINK}", ts="101.1")
    assert "nothing new or different was agreed" in app.slack.posted("chat.update")[-1]["text"]
    assert len(app.svc.store.requirements()) == 2 and app.issues_created == ["REQ-7", "REQ-8"]


def test_C_12_whitespace_only_edits_count_as_unchanged():
    notes = EditableNotes()
    gemini = Gemini(KICKOFF)
    app = App(gemini, fetch=notes)
    kickoff_approved(app)
    notes.text = NOTES.replace("\n", "   \n") + "\n\n"
    app.mention(f"capture {LINK}", ts="101.1")
    assert "haven't changed" in app.slack.posted("chat.update")[-1]["text"] and len(gemini.prompts) == 1


@pytest.mark.parametrize("words", ["analyse", "please check these notes", "here are today's notes:", ""])
def test_C_13_any_mention_with_a_notes_link_reads_the_notes(words):
    app = App()
    app.mention(f"{words} {LINK}".strip())
    assert "Reading the meeting notes" in app.slack.posted("chat.postMessage")[0]["text"]


@pytest.mark.parametrize("verb", ["analyse this", "Review this thread", "check this.", "sync"])
def test_C_14_other_verbs_capture_a_thread(verb):
    thread = [{"ts": "500.1", "user": "UMAYA", "text": "Uploads up to 25 MB."}]
    gemini = Gemini({"requirements": [], "open_questions": []})
    app = App(gemini, thread=thread)
    app.mention(verb, ts="500.2", thread_ts="500.1")
    assert len(gemini.prompts) == 1 and "Reading the thread" in app.slack.posted("chat.postMessage")[0]["text"]


def test_C_15_a_question_with_a_link_goes_to_the_agent_not_capture():
    asker = Asker()
    app = App(asker=asker)
    app.mention(f"why did we decide 25 MB in {LINK}?")
    assert asker.questions and not any("Reading the meeting notes" in m["text"] for m in app.slack.posted("chat.postMessage"))


def test_C_16_approve_shows_progress_before_the_issues_are_written():
    app = App()
    app.mention(f"capture {LINK}")
    card = app.last_card()
    app.click("approve_group", app.button(card, "approve_group"), card)
    progress, final = app.slack.posted("chat.update")[-2:]
    assert "Approving" in progress["text"] and "actions" not in json.dumps([b["type"] for b in progress["blocks"]])
    assert "PRD v1.0.0" in final["text"] and "Written to the PRD." in final["text"]


def test_C_17_without_a_prd_doc_the_card_does_not_claim_one_was_written():
    app = App()
    app.svc.prd = None
    kickoff_approved(app)
    note = app.slack.posted("chat.update")[-1]["text"]
    assert "PRD v1.0.0" in note and "Written to the PRD" not in note and "2 issues opened." in note


def test_C_18_approving_a_card_from_before_a_restart_says_capture_again():
    app = App()
    message = {"ts": "300.1", "blocks": [{"type": "actions", "elements": []}]}
    app.click("approve_proposal", "gone-id", message)
    assert "before Spec Keeper restarted" in app.slack.posted("chat.update")[-1]["text"]


def test_C_19_the_approval_note_links_the_prd_doc_and_the_issues():
    app = App()
    app.prd.doc_id, app.issues.repo = "PRDDOC", "acme/checkout"
    kickoff_approved(app)
    note = app.slack.posted("chat.update")[-1]["text"]
    assert "<https://docs.google.com/document/d/PRDDOC/edit|the PRD>" in note
    assert "<https://github.com/acme/checkout/issues?q=is%3Aissue+is%3Aopen+label%3Arequirement|2 issues>" in note


def test_C_20_notes_dismissed_in_full_are_read_again_when_asked():
    gemini = Gemini(KICKOFF, KICKOFF)
    app = App(gemini)
    app.mention(f"capture {LINK}")
    card = app.last_card()
    app.click("dismiss_group", app.button(card, "dismiss_group"), card)
    app.mention(f"capture {LINK}", ts="102.1")
    assert len(gemini.prompts) == 2 and "Approve all as v1.0.0" in json.dumps(app.last_card()["blocks"])
    card = app.last_card()
    app.click("approve_group", app.button(card, "approve_group"), card)
    assert app.issues_created == ["REQ-7", "REQ-8"]


def test_C_21_unchanged_notes_report_approved_and_dismissed_and_link_the_prd():
    app = App(Gemini(KICKOFF))
    app.prd.doc_id = "PRDDOC"
    app.mention(f"capture {LINK}")
    card = app.last_card()
    first = app.svc.store.proposals_in_group(app.button(card, "approve_group"))
    app.click("dismiss_proposal", first[1].id, {"ts": "200.1", "blocks": []})
    app.click("approve_proposal", first[0].id, {"ts": "200.2", "blocks": []})
    app.mention(f"capture {LINK}", ts="102.1")
    note = app.slack.posted("chat.update")[-1]["text"]
    assert "1 approved (REQ-7), 1 dismissed." in note
    assert f"<https://docs.google.com/document/d/PRDDOC/edit|v{app.svc.prd_version}>" in note


def _demo_app(**kw):
    from dataclasses import replace

    app = App(Gemini(KICKOFF, KICKOFF), **kw)
    app.http = TestClient(create_app(replace(settings(), demo_channels=("C1",)), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True))
    return app


def test_C_22_reset_demo_gives_the_next_person_a_clean_start():
    app = _demo_app()
    kickoff_approved(app)
    assert app.svc.prd_version == "1.0.0" and len(app.svc.store.requirements()) == 2
    app.mention("reset demo", ts="300.1")
    note = app.slack.posted("chat.postMessage")[-1]["text"]
    assert "Demo reset" in note and "2 requirements cleared" in note and "2 GitHub issues closed" in note
    assert app.svc.store.requirements() == [] and app.svc.prd_version == "" and app.issues.closed == [1, 1]
    assert app.prd.appended == []  # the PRD was rebuilt empty
    kickoff_approved(app)  # the same notes capture again, numbered from the start
    assert [r.key for r in app.svc.store.requirements()] == ["REQ-7", "REQ-8"] and app.svc.prd_version == "1.0.0"


def test_C_23_reset_only_works_in_the_demo_channel():
    app = App()
    kickoff_approved(app)
    app.mention("reset demo", ts="300.1")
    assert "only works in the demo channel" in app.slack.posted("chat.postMessage")[-1]["text"]
    assert len(app.svc.store.requirements()) == 2


# --- the change card's diff ----------------------------------------------------------------------


def _change_card(change: dict = CHANGE) -> tuple[App, dict]:
    app = App(Gemini(KICKOFF, change), thread=[{"ts": "200.1", "user": "UWEI", "text": "Cap uploads at 10 MB."}])
    kickoff_approved(app)
    app.mention("capture this", ts="200.3", thread_ts="200.1")
    return app, app.last_card()


def _lines(attachment: dict) -> list[list[dict]]:
    return [b["elements"][0]["elements"] for b in attachment["blocks"] if b["type"] == "rich_text"]


def test_C_24_a_change_card_shows_old_criteria_in_red_and_new_in_green_with_the_changed_words_marked():
    _, card = _change_card()
    red, green, footer = card["attachments"]
    assert (red["color"], green["color"]) == ("#cf222e", "#1a7f37")
    (old,), (new,) = _lines(red), _lines(green)
    assert old[0] == {"type": "text", "text": "- AC-7-1", "style": {"code": True}}
    assert new[0] == {"type": "text", "text": "+ AC-7-1", "style": {"code": True}}
    assert [r["text"] for r in old if r.get("style") == {"strike": True}] == ["30", "25"]
    assert [r["text"] for r in new if r.get("style") == {"bold": True}] == ["12", "10"]
    assert "".join(r["text"] for r in new[2:]) == "Given a 12 MB file When uploaded Then show 'File exceeds 10 MB'"
    assert footer["blocks"][-1]["type"] == "actions" and "color" not in footer  # buttons last, under the diff


def test_C_25_a_criterion_only_added_shows_in_green_alone_and_unchanged_ones_not_at_all():
    two = json.loads(json.dumps(CHANGE))
    two["requirements"][0]["acceptance_criteria"] = [
        {"given": "a 30 MB file", "when": "uploaded", "then": "show 'File exceeds 25 MB'"},  # as approved
        {"given": "a 9 MB file", "when": "uploaded", "then": "it is accepted"},
    ]
    _, card = _change_card(two)
    green, _footer = card["attachments"]  # nothing removed: no red block
    assert green["color"] == "#1a7f37"
    (line,) = _lines(green)
    assert line[0]["text"] == "+ AC-7-2" and all(r.get("style") in ({"bold": True}, None) for r in line[1:])


def test_C_26_approving_a_change_keeps_the_coloured_diff_and_swaps_the_buttons_for_the_outcome():
    app, card = _change_card()
    message = {"ts": card["ts"], "blocks": card["blocks"], "attachments": [{**a, "color": a.get("color", "").lstrip("#")} for a in card["attachments"]]}
    app.click("approve_proposal", app.button(card, "approve_proposal"), message)  # Slack sends colours without '#'
    final = app.slack.posted("chat.update")[-1]
    red, green, footer = final["attachments"]
    assert (red["color"], green["color"]) == ("#cf222e", "#1a7f37") and _lines(red) and _lines(green)
    assert "approve_proposal" not in json.dumps(final) and "REQ-7 is now v2" in json.dumps(footer)


def test_N_31_dismissing_a_change_clears_the_diff_and_its_buttons():
    app, card = _change_card()
    app.click("dismiss_proposal", app.button(card, "dismiss_proposal"), card)
    final = app.slack.posted("chat.update")[-1]
    assert final["attachments"] == [] and "Nothing was written" in final["text"]  # Slack keeps attachments unless told


def test_N_32_criterion_text_in_the_diff_is_literal_so_it_cannot_ping_or_link():
    evil = json.loads(json.dumps(CHANGE))
    evil["requirements"][0]["acceptance_criteria"][0]["then"] = "show <!channel> and <https://evil.test|10 MB>"
    _, card = _change_card(evil)
    red, green, _ = card["attachments"]
    assert {b["type"] for b in red["blocks"] + green["blocks"]} == {"rich_text"}  # no mrkdwn to parse
    texts = [r for line in _lines(green) for r in line]
    assert all(r["type"] == "text" for r in texts) and "<!channel>" in "".join(r["text"] for r in texts)


def test_N_33_small_talk_gets_help_not_a_gemini_call():
    asker = Asker()
    app = App(asker=asker)
    for i, text in enumerate(["hello", "thanks!", "help", ""]):
        app.mention(text, ts=f"30{i}.1")
    assert asker.questions == []
    assert all("capture this" in m["text"] for m in app.slack.posted("chat.postMessage"))


def test_C_27_a_bare_notes_link_still_captures_when_the_agent_is_on():
    asker = Asker()
    app = App(asker=asker)
    app.mention(LINK)
    assert asker.questions == [] and "Reading the meeting notes" in app.slack.posted("chat.postMessage")[0]["text"]


def test_C_31_approving_a_change_card_that_went_out_of_date_says_why_and_writes_nothing():
    to_5 = json.loads(json.dumps(CHANGE).replace("10 MB", "5 MB").replace("12 MB", "7 MB"))
    app = App(Gemini(KICKOFF, CHANGE, to_5), thread=[{"ts": "200.1", "user": "UWEI", "text": "Cap uploads at 10 MB."}])
    kickoff_approved(app)
    app.mention("capture this", ts="200.3", thread_ts="200.1")
    ten = app.last_card()
    app.mention("capture this", ts="200.4", thread_ts="200.1")
    five = app.last_card()
    app.click("approve_proposal", app.button(ten, "approve_proposal"), ten)
    app.click("approve_proposal", app.button(five, "approve_proposal"), five)
    final = app.slack.posted("chat.update")[-1]
    assert "REQ-7 is at v2 now" in final["text"] and "Nothing was written" in final["text"]
    assert "approve_proposal" not in json.dumps(final)  # no buttons to try again with
    r = app.svc.store.get_requirement("REQ-7")
    assert (r.version, r.title) == (2, "Receipt uploads are limited to 10 MB")


def test_N_38_notes_someone_else_is_capturing_get_a_wait_message_and_no_gemini_call():
    gemini = Gemini(KICKOFF)
    app = App(gemini)
    assert app.svc.store.claim_lock(f"notes-{DOC}", 60)  # another instance is reading them
    app.mention(f"capture {LINK}")
    assert gemini.prompts == []
    assert "Someone is reading these notes right now" in app.slack.posted("chat.update")[-1]["text"]


def test_N_39_cards_have_no_edit_button_and_an_old_card_s_edit_still_gets_an_answer():
    _, card = _change_card()
    assert "edit_proposal" not in json.dumps(card)
    app = App()
    app.mention(f"capture {LINK}")
    app.click("review_group", app.button(app.last_card(), "review_group"), app.last_card())
    cards = [m for m in app.slack.posted("chat.postMessage") if "approve_proposal" in json.dumps(m)]
    assert cards and not any("edit_proposal" in json.dumps(m) for m in cards)
    old = {"ts": "1.1", "blocks": []}  # a card posted before Edit was taken off
    assert app.click("edit_proposal", "p1", old).status_code == 200
    (note,) = app.slack.posted("chat.postEphemeral")
    assert "Dismiss this card" in note["text"] and app.svc.store.get_proposal("p1") is None


def test_C_33_review_one_by_one_on_a_reposted_card_posts_the_cards_in_that_thread():
    app = App(Gemini(KICKOFF))
    app.mention(f"capture {LINK}")  # captured in thread 100.1
    first = app.last_card()
    app.click("review_group", app.button(first, "review_group"), first)
    one = next(m for m in app.slack.posted("chat.postMessage") if "approve_proposal" in json.dumps(m))
    app.click("approve_proposal", app.button({"blocks": one["blocks"]}, "approve_proposal"), {"ts": "1.9", "blocks": one["blocks"]})

    app.mention(f"capture {LINK}", ts="300.1")  # the link again, in a new thread
    again = app.slack.posted("chat.update")[-1]
    assert "1 already approved: REQ-7" in json.dumps(again["blocks"])  # on the card, not only in notifications
    assert "captured 1 agreed requirement:" in json.dumps(again["blocks"])
    card = {"ts": again["ts"], "thread_ts": "300.1", "blocks": again["blocks"]}
    before = len(app.slack.posted("chat.postMessage"))
    app.click("review_group", app.button(card, "review_group"), card)
    (posted,) = [m for m in app.slack.posted("chat.postMessage")[before:] if "approve_proposal" in json.dumps(m)]
    assert posted["thread_ts"] == "300.1"  # below the card clicked, not in thread 100.1


@pytest.mark.parametrize("text", ["", "﻿\n\n   \n", "* \n-\n•\n"])
def test_N_40_an_empty_doc_gets_a_plain_reply_and_no_gemini_call(text):
    gemini = Gemini(KICKOFF)
    app = App(gemini, fetch=lambda doc_id: ("Planning notes", text))
    app.mention(f"capture {LINK}")
    assert gemini.prompts == []
    assert "That doc looks empty" in app.slack.posted("chat.update")[-1]["text"]
    assert app.svc.store.get_meta(f"doc:{DOC}") == ""  # not remembered as captured


def test_C_35_a_doc_captured_empty_then_filled_in_gets_the_meeting_card():
    notes = EditableNotes("")
    app = App(Gemini(KICKOFF), fetch=notes)
    app.mention(f"capture {LINK}")
    notes.text = NOTES
    app.mention(f"capture {LINK}", ts="300.1")
    card = json.dumps(app.last_card()["blocks"])
    assert "captured 2 agreed requirements" in card and "approve_group" in card
    assert "were edited" not in app.slack.posted("chat.update")[-1]["text"]


def test_C_36_a_doc_recorded_empty_before_this_fix_is_a_first_capture_once_filled_in():
    gemini = Gemini(KICKOFF)
    app = App(gemini)
    app.svc.store.set_meta(f"doc:{DOC}", "gEMPTY")  # an older server read it empty and kept a group with nothing in it
    app.svc.store.set_meta(f"dochash:{DOC}", "old")
    app.mention(f"capture {LINK}")
    assert "approve_group" in json.dumps(app.last_card()["blocks"]) and len(gemini.prompts) == 1
