"""The landing page judges open first."""

from dataclasses import replace

from fastapi.testclient import TestClient
from test_e2e import App, fetch_ok, settings

from speckeeper.main import create_app


def page(**changes) -> TestClient:
    app = App()
    return TestClient(create_app(replace(settings(), **changes), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True))


def test_the_root_explains_the_product_with_live_links_and_limits():
    http = page(prd_doc_id="DOC1", github_repo="taqelah/checkout-app", requests_per_user=7, requests_window_seconds=300)
    res = http.get("/")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    body = res.text
    assert "Specs can’t keep up." in body and "The problem" in body and "AC-7-1@v1" in body
    assert "youtube-nocookie.com/embed/xVQnzTEOaO4" in body and body.index('id="demo"') < body.index('id="problem"')  # the demo video, before the problem
    assert "https://docs.google.com/document/d/DOC1/edit" in body and "taqelah/checkout-app/actions" in body
    assert "7 requests every 5 minutes" in body and "$" not in body.split("</style>")[1]  # every placeholder filled
    assert "6 · Watch the tests go stale." in body and "taqelah/checkout-app/actions" in body.split("6 · Watch the tests go stale.")[1][:600]
    assert "9 · Undo a mistake." in body and 'data-copy="@Spec Keeper retire REQ-11 wrong notes"' in body
    assert "If you hit a limit." in body and "each person gets 7 every 5 minutes; approving, dismissing and retiring don’t count" in body
    assert all(http.get(f).status_code == 200 for f in ("/architecture.png", "/change-sequence.png", "/icon.png"))


def test_buttons_appear_only_for_links_that_are_set():
    body = page().get("/").text
    assert "Join our Slack to try it" not in body and "Watch the demo" not in body and "See the code" in body
    body = page(demo_slack_invite="https://join.slack.com/t/x/shared_invite/abc", demo_video_url="https://youtu.be/v", contact_email="a@b.co").get("/").text
    assert "Join our Slack to try it" in body and "Watch the demo" in body and "mailto:a@b.co" in body
    assert "javascript:" not in page(demo_slack_invite="javascript:alert(1)").get("/").text


def test_the_status_line_shows_live_state_from_the_store():
    from test_e2e import junit, kickoff_approved

    app = App()
    kickoff_approved(app)
    app.ci(junit(("AC-7-1@v1 shows the limit", True)))
    body = TestClient(create_app(settings(), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True)).get("/").text
    assert "Live on Cloud Run" in body and "PRD <b>v1.0.0</b>" in body and "<b>2</b> requirements" in body
    assert "1 passing" in body and "not tested yet" in body and "last approval" in body


def test_the_status_line_never_breaks_the_page():
    app = App()
    app.svc.store.requirements = lambda: (_ for _ in ()).throw(RuntimeError("Firestore down"))
    body = TestClient(create_app(settings(), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True)).get("/").text
    assert "Live on Cloud Run" in body and "Specs can’t keep up." in body


def test_copy_buttons_carry_the_test_inputs():
    body = page(demo_notes_url="https://docs.google.com/document/d/NOTES/edit").get("/").text
    assert 'data-copy="let&#x27;s cap uploads at 8 MB"' in body or 'data-copy="let\'s cap uploads at 8 MB"' in body
    assert (
        'data-copy="@Spec Keeper https://docs.google.com/document/d/NOTES/edit"' in body
        and 'data-copy="@Spec Keeper reset demo"' in body
        and "Grounding, guardrails and security" in body
        and "Built on Google Cloud, tuned for speed" in body
    )


def test_the_status_line_reads_well_on_a_clean_start():
    body = page().get("/").text
    assert "clean start: nothing approved yet" in body


def test_a_just_started_instance_serves_the_page_not_an_empty_body(monkeypatch):
    # A fresh Cloud Run instance's monotonic clock can read a few seconds; the first visitors still get the page.
    import speckeeper.main as main

    monkeypatch.setattr(main.time, "monotonic", lambda: 5.0)
    res = page().get("/")
    assert res.status_code == 200 and "Specs can’t keep up." in res.text


def test_try_it_explains_joining_slack_step_by_step_when_there_is_an_invite():
    body = page(demo_slack_invite="https://join.slack.com/t/x/shared_invite/abc", demo_channel_name="project-checkout").get("/").text
    assert "Join the demo Slack" in body and 'href="https://join.slack.com/t/x/shared_invite/abc"' in body
    assert "six-character code" in body and "Browse channels" in body and "<b>project-checkout</b>" in body
    assert "About fifteen minutes in #project-checkout" in body and "Start clean</b> in #project-checkout" in body
    assert "Join the demo Slack" not in page().get("/").text  # no invite link: no join steps


def test_the_reset_hint_offers_the_video_only_when_there_is_one():
    assert "watch the demo video" not in page().get("/").text
    assert 'href="https://youtu.be/v"' in page(demo_video_url="https://youtu.be/v").get("/").text.split("Start clean")[1]


def test_try_it_shows_memory_bank_and_trust_names_recovery_and_supply_chain():
    body = page().get("/").text
    assert 'data-copy="@Spec Keeper what is the upload limit?"' in body and "3 · Ask a question</b> in the same thread" in body
    assert "7 · Ask why." in body and "changed since you last asked" in body and "8 · Try to break it" in body
    assert "Recoverable and audited." in body and "point-in-time recovery" in body and "Supply chain." in body


def test_every_link_off_the_page_opens_in_a_new_tab():
    import re

    body = (
        page(
            prd_doc_id="DOC1",
            github_repo="taqelah/checkout-app",
            demo_slack_invite="https://join.slack.com/t/x/shared_invite/abc",
            demo_video_url="https://youtu.be/v",
            contact_email="a@b.co",
            demo_notes_url="https://docs.google.com/document/d/N/edit",
        )
        .get("/")
        .text
    )
    links = re.findall(r"<a [^>]*>", body)
    away = [a for a in links if not re.search(r'href="#', a)]
    assert len(away) > 15 and all('target="_blank"' in a and 'rel="noopener"' in a for a in away)
    assert all(a.count("target=") <= 1 for a in links)  # never added twice
    assert '<a class="brand" href="#top">' in body  # links within the page stay in the tab
