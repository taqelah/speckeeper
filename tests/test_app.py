"""The web service boots without credentials and guards /ci; no network calls."""

from fastapi.testclient import TestClient

from speckeeper.config import Settings
from speckeeper.main import create_app
from speckeeper.service import SpecKeeper
from speckeeper.store import MemoryStore


def settings(**kw):
    base = dict(slack_bot_token="xoxb-test", slack_signing_secret="secret", gcp_project="p", gcp_location="l", gemini_location="global", pro_model="m",
                flash_model="m", prd_doc_id="", github_token="", github_repo="", ci_secret="s3cret", store="memory")
    return Settings(**{**base, **kw})


def client():
    svc = SpecKeeper(MemoryStore(), lambda prompt, schema: {"requirements": []})
    return TestClient(create_app(settings(), svc))


def test_health():
    assert client().get("/health").json() == {"ok": True} == client().get("/healthz").json()


def test_ci_rejects_a_wrong_secret():
    res = client().post("/ci", json={"run_id": "1", "channel": "C1", "junit_xml": "<testsuites/>"}, headers={"X-SpecKeeper-Secret": "nope"})
    assert res.status_code == 401


def test_ci_with_no_matching_requirements_posts_nothing():
    res = client().post("/ci", json={"run_id": "1", "channel": "C1", "junit_xml": "<testsuites><testcase name='x'/></testsuites>"}, headers={"X-SpecKeeper-Secret": "s3cret"})
    assert res.json() == {"requirements": [], "unmapped": ["x"]}


def test_slack_rejects_unsigned_requests():
    assert client().post("/slack/events", json={"type": "url_verification", "challenge": "c"}).status_code == 401


def test_no_public_api_listing():
    c = client()
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
