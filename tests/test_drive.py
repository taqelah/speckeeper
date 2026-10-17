"""Google Drive: every call asks the client to retry (a connection left open between requests can be gone,
and the next one fails with a broken pipe), the public-link fallback is only for a doc Drive can't see,
and Slack blames sharing only when sharing is the problem."""

import sys
import types

import pytest
from test_e2e import LINK, App

from speckeeper import main, notes
from speckeeper.notes import DRIVE_RETRIES


class Call:
    def __init__(self, log, result, error=None):
        self.log, self.result, self.error = log, result, error

    def execute(self, **kw):
        self.log.append(kw)
        if self.error:
            raise self.error
        return self.result


class FakeFiles:
    def __init__(self, log, error=None):
        self.log, self.error = log, error

    def get(self, **_):
        return Call(self.log, {"name": "Kickoff"}, self.error)

    def export(self, **_):
        return Call(self.log, b"* Decided: 25 MB")

    def update(self, **_):
        return Call(self.log, {})


class FakeDrive:
    def __init__(self, log, error=None):
        self.log, self.error = log, error

    def files(self):
        return FakeFiles(self.log, self.error)


class NoAccess(Exception):
    """Shaped like googleapiclient's HttpError: Drive's answer is on .resp.status."""

    def __init__(self, status):
        super().__init__(f"HttpError {status}")
        self.resp = types.SimpleNamespace(status=status)


@pytest.fixture
def fake_google(monkeypatch):
    log = []
    auth = types.ModuleType("google.auth")
    auth.default = lambda scopes=None: (object(), "p")
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *a, **k: FakeDrive(log)
    http = types.ModuleType("googleapiclient.http")
    http.MediaInMemoryUpload = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    import google

    monkeypatch.setattr(google, "auth", auth, raising=False)
    return log


def test_C_51_every_drive_call_asks_for_retries(fake_google):
    assert notes.drive_fetch()("DOC") == ("Kickoff", "* Decided: 25 MB")
    from speckeeper.integrations import GoogleDocsPRD

    GoogleDocsPRD("PRD").publish([], [], "1.0.0", "now")
    assert len(fake_google) == 4 and all(kw == {"num_retries": DRIVE_RETRIES} for kw in fake_google)


def _lazy(monkeypatch, error):
    public = []
    monkeypatch.setattr(notes, "drive_fetch", lambda: lambda doc_id: (_ for _ in ()).throw(error))
    monkeypatch.setattr(notes, "public_fetch", lambda doc_id: public.append(doc_id) or ("Kickoff", "text"))
    return main.lazy_drive_fetch(), public


@pytest.mark.parametrize("error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(), NoAccess(500), TimeoutError()])
def test_N_50_when_drive_is_unreachable_the_public_link_is_not_tried(monkeypatch, error):
    fetch, public = _lazy(monkeypatch, error)
    with pytest.raises(type(error)):
        fetch("DOC")
    assert public == []  # its "not shared by link" would have hidden the real error


@pytest.mark.parametrize("status", [403, 404])
def test_C_52_when_drive_cant_see_the_doc_the_public_link_is_tried(monkeypatch, status):
    fetch, public = _lazy(monkeypatch, NoAccess(status))
    assert fetch("DOC") == ("Kickoff", "text") and public == ["DOC"]


def test_N_51_a_dropped_connection_is_not_reported_as_a_sharing_problem():
    def broken(doc_id):
        raise BrokenPipeError(32, "Broken pipe")

    app = App(fetch=broken)
    app.mention(f"capture {LINK}")
    text = app.slack.posted("chat.update")[-1]["text"]
    assert "Share the doc" not in text and text == "I couldn't read those notes just now. Try again in a minute."


@pytest.mark.parametrize("error", [PermissionError("doc is not shared by link"), NoAccess(403), NoAccess(404)])
def test_C_53_a_doc_nobody_shared_still_says_how_to_share_it(error):
    def denied(doc_id):
        raise error

    app = App(fetch=denied)
    app.mention(f"capture {LINK}")
    assert "Share the doc with Spec Keeper's service account" in app.slack.posted("chat.update")[-1]["text"]
