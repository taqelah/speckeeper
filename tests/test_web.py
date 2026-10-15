"""The landing page judges open first."""

from dataclasses import replace

from fastapi.testclient import TestClient

from speckeeper.main import create_app
from test_e2e import App, fetch_ok, settings


def page(**changes) -> TestClient:
    app = App()
    return TestClient(create_app(replace(settings(), **changes), app.svc, slack_client=app.slack.client(), fetch_doc=fetch_ok, wait=True))


def test_the_root_explains_the_product_with_live_links_and_limits():
    http = page(prd_doc_id="DOC1", github_repo="taqelah/checkout-app", requests_per_user=7, requests_window_seconds=300)
    res = http.get("/")
    assert res.status_code == 200 and "text/html" in res.headers["content-type"]
    body = res.text
    assert "Nobody told the tests." in body and "Where to look for each criterion" in body and "AC-7-1@v1" in body
    assert "https://docs.google.com/document/d/DOC1/edit" in body and "taqelah/checkout-app/actions" in body
    assert "7 requests every 5 minutes" in body and "$" not in body.split("</style>")[1]  # every placeholder filled
    assert all(http.get(f).status_code == 200 for f in ("/architecture.png", "/change-sequence.png", "/icon.png"))


def test_buttons_appear_only_for_links_that_are_set():
    body = page().get("/").text
    assert "Join the demo Slack" not in body and "Watch the demo" not in body and "See the code" in body
    body = page(demo_slack_invite="https://join.slack.com/t/x/shared_invite/abc", demo_video_url="https://youtu.be/v", contact_email="a@b.co").get("/").text
    assert "Join the demo Slack" in body and "Watch the demo" in body and "mailto:a@b.co" in body
    assert "javascript:" not in page(demo_slack_invite="javascript:alert(1)").get("/").text
