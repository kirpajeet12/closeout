"""The office's Microsoft 365 or Outlook mailbox, connected with Microsoft's sign-in. Microsoft itself is a fake Graph in
memory, so these tests cover the real client code: the sign-in, sending with the report attached, and reading replies."""

from __future__ import annotations

import dataclasses
import re
import types
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from closeout import api, gmail as gmail_mod, inbox as inbox_mod, outlook as outlook_mod, pipeline
from closeout.store import Store
from tests.test_api import FakeAgent
from tests.test_inbox import _wait_for_run
from tests.test_review import _jpeg_bytes, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review

OFFICE = "office@example.com"


class FakeGraph:
    """Microsoft's sign-in and mail API for one mailbox. Graph ids look like g1; Message-IDs like <1@outlook.test>."""

    def __init__(self):
        self.messages: list[dict] = []
        self.tokens = 0
        self.searches: list[str] = []

    # --- mailbox contents ---
    def _add(self, raw: bytes, folder: str, conversation: str, message_id: str = "", subject: str = "") -> dict:
        n = len(self.messages) + 1
        m = {"id": f"g{n}", "internetMessageId": message_id or f"<{n}@outlook.test>", "conversationId": conversation,
             "folder": folder, "raw": raw, "subject": subject, "receivedDateTime": "2026-09-14T16:00:00Z", "attachments": []}
        self.messages.append(m)
        return m

    def arrive(self, from_addr: str, subject: str, text: str, conversation: str, files=(), in_reply_to: str = "") -> dict:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = from_addr, OFFICE, subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        msg.set_content(text)
        for name, data in files:
            msg.add_attachment(data, maintype="image", subtype="jpeg", filename=name)
        return self._add(msg.as_bytes(), "inbox", conversation, subject=subject)

    def _by_id(self, gid: str) -> dict:
        return next(m for m in self.messages if m["id"] == gid)

    # --- http ---
    def _reply(self, method: str, url: str, status: int = 200, **kw) -> httpx.Response:
        return httpx.Response(status, request=httpx.Request(method, url), **kw)

    def post(self, url, data=None, timeout=None):
        assert url.startswith(outlook_mod.LOGIN) and data["client_secret"] == "ms-secret"
        if data["grant_type"] == "authorization_code" and data.get("code") != "good":
            return self._reply("POST", url, 400, json={"error": "invalid_grant"})
        self.tokens += 1
        return self._reply("POST", url, json={"access_token": f"at-{self.tokens}", "refresh_token": f"rt-{self.tokens}"})

    def put(self, url, content=None, headers=None, timeout=None):
        return self._reply("PUT", url, 201, json={})

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer at-")
        path = url.removeprefix(outlook_mod.API)
        params = params or {}
        if method == "GET" and path == "":
            return self._reply(method, url, json={"mail": OFFICE})
        if method == "POST" and path == "/messages":
            n = len(self.messages) + 1
            draft = self._add(b"", "drafts", f"conv-{n}", subject=json["subject"])
            draft["draft"] = json
            return self._reply(method, url, 201, json={k: draft[k] for k in ("id", "internetMessageId", "conversationId")})
        if m := re.fullmatch(r"/messages/(g\d+)/attachments", path):
            self._by_id(m[1])["attachments"].append((json["name"], json["contentType"]))
            return self._reply(method, url, 201, json={})
        if m := re.fullmatch(r"/messages/(g\d+)/send", path):
            d = self._by_id(m[1])
            to = d["draft"]["toRecipients"][0]["emailAddress"]["address"]
            raw = gmail_mod.build_message(OFFICE, to, d["draft"]["subject"], d["draft"]["body"]["content"]).as_bytes()
            d.update(folder="sent", raw=raw)
            if to.lower() == OFFICE:       # sent to itself: a second copy lands in the inbox, same Message-ID, new Graph id
                self._add(raw, "inbox", d["conversationId"], d["internetMessageId"], d["subject"])
            return self._reply(method, url, 202)
        if path == "/mailFolders/inbox/messages":
            inbox = [m for m in self.messages if m["folder"] == "inbox"]
            if f := params.get("$filter"):
                field, value = re.fullmatch(r"(\w+) eq '(.*)'", f).groups()
                inbox = [m for m in inbox if m[field] == value.replace("''", "'")]
            if s := params.get("$search"):
                self.searches.append(s)
                ref = s.strip('"').split(":", 1)[1]
                inbox = [m for m in inbox if ref in m["subject"]]
            return self._reply(method, url, json={"value": [{k: m[k] for k in ("id", "internetMessageId", "conversationId", "subject",
                                                                               "receivedDateTime")} for m in inbox]})
        if m := re.fullmatch(r"/messages/(g\d+)/\$value", path):
            return self._reply(method, url, content=self._by_id(m[1])["raw"])
        if m := re.fullmatch(r"/messages/(g\d+)", path):
            return self._reply(method, url, json={k: self._by_id(m[1])[k] for k in ("id", "conversationId")})
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(outlook_mod, "httpx", types.SimpleNamespace(post=g.post, put=g.put, request=g.request, Response=httpx.Response))
    return g


@pytest.fixture
def mclient(client, graph):
    """The same data served by an app whose server carries a Microsoft app registration."""
    settings = dataclasses.replace(client.settings, microsoft_client_id="ms-client", microsoft_client_secret="ms-secret",
                                   mail_check_seconds=0)
    c = TestClient(api.create_app(settings))
    c.settings = settings
    c.post("/signin", data={"code": settings.access_code}) if settings.access_code else None
    return c


def _connect(c) -> None:
    loc = c.get("/api/mail/connect/microsoft", follow_redirects=False).headers["location"]
    state = parse_qs(urlparse(loc).query)["state"][0]
    r = c.get(f"/api/mail/callback/microsoft?code=good&state={state}", follow_redirects=False)
    assert r.headers["location"].endswith("mail=connected")


def test_signing_in_with_microsoft_keeps_the_address_and_the_newest_token_only(mclient, graph):
    assert mclient.get("/api/mail").json()["microsoft"] is True
    r = mclient.get("/api/mail/connect/microsoft", follow_redirects=False)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "login.microsoftonline.com" and loc.path == "/common/oauth2/v2.0/authorize"
    assert q["client_id"] == ["ms-client"] and q["redirect_uri"][0].endswith("/api/mail/callback/microsoft")
    assert {"offline_access", "Mail.Send", "Mail.ReadWrite"} <= set(q["scope"][0].split())
    assert "ms-secret" not in r.headers["location"]
    # a wrong state, a refusal, or a code Microsoft will not take connects nothing
    assert mclient.get("/api/mail/callback/microsoft?code=good&state=wrong", follow_redirects=False).headers["location"].endswith("mail=refused")
    assert mclient.get(f"/api/mail/callback/microsoft?error=access_denied&state={q['state'][0]}",
                       follow_redirects=False).headers["location"].endswith("mail=refused")
    assert mclient.get(f"/api/mail/callback/microsoft?code=bad&state={q['state'][0]}",
                       follow_redirects=False).headers["location"].endswith("mail=failed")
    assert mclient.get("/api/mail").json()["account"] is None
    _connect(mclient)
    j = mclient.get("/api/mail").json()
    assert j["account"]["address"] == OFFICE and j["account"]["kind"] == "outlook" and j["account"]["ready"]
    assert "rt-" not in str(j) and "at-" not in str(j)
    # Microsoft rotated the token while reading the address; the rotated one is what is kept
    assert Store(mclient.settings.data_dir / "closeout.db").mail_account()["refresh_token"] == f"rt-{graph.tokens}"
    assert mclient.delete("/api/mail").json()["account"] is None


def test_the_items_go_out_from_microsoft_and_the_reply_in_that_conversation_is_filed(mclient, graph, tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    slug, rev, _ = _finished_review(mclient, tmp_path)
    _connect(mclient)
    assert mclient.get(f"/api/projects/{slug}").json()["mail"]["gmail"] == OFFICE
    r = mclient.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com"})
    assert r.status_code == 200, r.text
    s = r.json()["send"]
    sent = next(m for m in graph.messages if m["folder"] == "sent")
    assert s["via"] == "email" and s["thread_id"] == sent["conversationId"]
    assert sent["attachments"] and sent["attachments"][0][1] == "application/pdf"
    graph.arrive("site@contractor.com", "RE: " + s["subject"], "Cover plate installed, photo attached.\n\n> earlier text",
                 sent["conversationId"], [("IMG_0021.jpg", _jpeg_bytes())], in_reply_to=sent["internetMessageId"])
    assert mclient.post("/api/mail/check").json()["check"] == {"looked_at": 1, "new": 1, "placed": 1, "unplaced": 0}
    assert _wait_for_run(mclient)["status"] == "done"
    inbound = mclient.get(f"/api/projects/{slug}").json()["inbound"]
    assert len(inbound) == 1 and inbound[0]["review_id"] == rev["id"] and inbound[0]["files"] == 1
    assert inbound[0]["text"].startswith("Cover plate installed")
    assert mclient.post("/api/mail/check").json()["check"]["new"] == 0
    # a new email carrying the review's reference in its subject, outside the conversation, is found too
    graph.arrive("pm@builder.com", f"Photos for {inbox_mod.review_ref(rev['id'])}", "Two more.", "conv-other", [("b.jpg", _jpeg_bytes())])
    assert mclient.post("/api/mail/check").json()["check"]["new"] == 1
    assert any(inbox_mod.review_ref(rev["id"]) in q for q in graph.searches)


def test_a_message_sent_to_the_office_itself_is_not_mistaken_for_the_reply(mclient, graph, tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    slug, rev, _ = _finished_review(mclient, tmp_path)
    _connect(mclient)
    s = mclient.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": OFFICE}).json()["send"]
    copy = next(m for m in graph.messages if m["folder"] == "inbox")
    assert copy["conversationId"] == s["thread_id"]
    # the inbox copy of the office's own message is not a reply
    assert mclient.post("/api/mail/check").json()["check"]["new"] == 0
    # the office answering from the same address, trying Closeout out, is
    graph.arrive(OFFICE, "RE: " + s["subject"], "I did it.", s["thread_id"], in_reply_to=copy["internetMessageId"])
    out = mclient.post("/api/mail/check").json()["check"]
    assert out["new"] == 1 and out["placed"] == 1
    inbound = mclient.get(f"/api/projects/{slug}").json()["inbound"]
    assert len(inbound) == 1 and inbound[0]["text"].startswith("I did it.")


def test_without_a_microsoft_app_the_button_is_hidden_and_nothing_connects(client):
    assert client.get("/api/mail").json()["microsoft"] is False
    assert client.get("/api/mail/connect/microsoft", follow_redirects=False).status_code == 409
