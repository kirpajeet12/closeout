"""The office's Gmail, connected once. Closeout sends from it on the engineer's press, reads replies in its own threads
and mail labelled for it, files what comes back to the right review, and asks the engineer about anything it cannot place."""

from __future__ import annotations

import dataclasses
import time
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from closeout import api, gmail as gmail_mod, inbox as inbox_mod, pipeline
from closeout.store import Store
from tests.test_api import FakeAgent
from tests.test_review import FakeFieldAgent, _jpeg_bytes, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


def _raw(from_addr: str, subject: str, text: str, files: list[tuple[str, bytes]] = ()) -> bytes:
    m = EmailMessage()
    m["From"] = f"Site Super <{from_addr}>"
    m["To"] = "office@example.com"
    m["Subject"] = subject
    m["Date"] = "Sat, 12 Sep 2026 09:15:00 -0700"
    m.set_content(text)
    for name, data in files:
        m.add_attachment(data, maintype="image", subtype="jpeg", filename=name)
    return m.as_bytes()


class FakeGmail:
    """Stands in for Google: a mailbox in memory, message ids like m1, threads like t1."""
    mailbox: dict[str, dict] = {}   # id -> {raw, thread, labels}
    sent: list[dict] = []
    labels: list[str] = []
    address = "office@example.com"

    def __init__(self, settings, refresh_token, address=""):
        self.address = address or FakeGmail.address

    @classmethod
    def reset(cls):
        cls.mailbox, cls.sent, cls.labels = {}, [], []

    @classmethod
    def arrive(cls, raw: bytes, thread: str = "", labels: tuple[str, ...] = ()) -> str:
        mid = f"m{len(cls.mailbox) + 1}"
        cls.mailbox[mid] = {"raw": raw, "thread": thread or f"t{mid}", "labels": list(labels)}
        return mid

    def profile(self):
        return self.address

    def send(self, to, subject, body, attachments=()):
        mid = f"m{len(FakeGmail.mailbox) + 1}"
        raw = gmail_mod.build_message(self.address, to, subject, body, attachments).as_bytes()
        FakeGmail.mailbox[mid] = {"raw": raw, "thread": f"t{mid}", "labels": ["SENT"]}
        FakeGmail.sent.append({"to": to, "subject": subject, "body": body, "id": mid, "attachments": [(n, len(b), m) for n, b, m in attachments]})
        return {"id": mid, "thread_id": f"t{mid}"}

    def search(self, query, limit=50):
        want = query.split()[0].split(":", 1)[1]
        skip = query.split()[1].split(":", 1)[1] if len(query.split()) > 1 else None
        return [i for i, m in FakeGmail.mailbox.items() if want in m["labels"] and skip not in m["labels"]]

    def thread_message_ids(self, thread_id):
        return [i for i, m in FakeGmail.mailbox.items() if m["thread"] == thread_id]

    def message(self, message_id):
        m = FakeGmail.mailbox[message_id]
        inc = gmail_mod.parse_raw(m["raw"], self.address)
        inc.id, inc.thread_id = message_id, m["thread"]
        return inc

    def mark(self, message_id, add, remove=""):
        FakeGmail.mailbox[message_id]["labels"].append(add)
        FakeGmail.labels.append((message_id, add))


@pytest.fixture
def gclient(client, monkeypatch):
    """The same data served by an app whose server carries a Google client; Google itself is the fake above."""
    FakeGmail.reset()
    monkeypatch.setattr(gmail_mod, "Gmail", FakeGmail)
    monkeypatch.setattr(gmail_mod, "exchange_code", lambda settings, code, redirect: {"refresh_token": f"rt-{code}"} if code == "good" else {})
    settings = dataclasses.replace(client.settings, google_client_id="cid", google_client_secret="secret", mail_check_seconds=0)
    c = TestClient(api.create_app(settings))
    c.settings = settings
    c.post("/signin", data={"code": settings.access_code}) if settings.access_code else None
    return c


def _wait_for_run(c):
    for _ in range(200):
        runs = Store(c.settings.data_dir / "closeout.db").runs(kind="batch")
        if runs and runs[-1]["status"] in ("done", "failed"):
            return runs[-1]
        time.sleep(0.05)
    raise AssertionError("the filing run never finished")


def test_connecting_goes_through_google_and_keeps_only_the_address_in_view(gclient):
    assert gclient.get("/api/mail").json()["account"] is None
    r = gclient.get("/api/mail/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/") and "gmail.modify" in r.headers["location"]
    state = r.headers["location"].split("state=")[1].split("&")[0]
    # a callback with a wrong state or a refusal connects nothing
    bad = gclient.get("/api/mail/callback?code=good&state=wrong", follow_redirects=False)
    assert bad.headers["location"].endswith("mail=refused") and gclient.get("/api/mail").json()["account"] is None
    ok = gclient.get(f"/api/mail/callback?code=good&state={state}", follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"].endswith("mail=connected")
    j = gclient.get("/api/mail").json()
    assert j["configured"] and j["account"]["address"] == "office@example.com" and j["label"] == "Closeout"
    assert "refresh_token" not in str(j)
    assert gclient.delete("/api/mail").json()["account"] is None


def _connected(gclient):
    state = gclient.get("/api/mail/connect", follow_redirects=False).headers["location"].split("state=")[1].split("&")[0]
    gclient.get(f"/api/mail/callback?code=good&state={state}", follow_redirects=False)


def test_the_message_goes_out_from_the_connected_address_and_the_reply_comes_back_to_its_review(gclient, tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    slug, rev, _ = _finished_review(gclient, tmp_path)
    gclient.post(f"/api/projects/{slug}/reviews/{rev['id']}/share")
    _connected(gclient)
    assert gclient.get(f"/api/projects/{slug}").json()["mail"]["gmail"] == "office@example.com"
    r = gclient.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com"})
    assert r.status_code == 200, r.text
    s = r.json()["send"]
    assert s["via"] == "gmail" and s["thread_id"] == "tm1" and FakeGmail.sent[0]["to"] == "site@contractor.com" and "/c/" in FakeGmail.sent[0]["body"]
    # the contractor replies in the thread with a photo: Closeout finds it, files it to this review, keeps their words
    FakeGmail.arrive(_raw("site@contractor.com", "Re: " + s["subject"], "Cover plate installed, photo attached.\n\n> earlier text",
                          [("IMG_0021.jpg", _jpeg_bytes())]), thread="tm1")
    r = gclient.post("/api/mail/check")
    assert r.status_code == 200, r.text
    assert r.json()["check"] == {"looked_at": 1, "new": 1, "placed": 1, "unplaced": 0}
    run = _wait_for_run(gclient)
    assert run["status"] == "done"
    p = gclient.get(f"/api/projects/{slug}").json()
    inbound = p["inbound"]
    assert len(inbound) == 1 and inbound[0]["review_id"] == rev["id"] and inbound[0]["how"] == "thread"
    assert inbound[0]["status"] == "placed" and inbound[0]["files"] == 1 and inbound[0]["from_addr"] == "site@contractor.com"
    assert inbound[0]["text"].startswith("Cover plate installed")
    assert p["batches"][-1]["via"] == f"email:{inbound[0]['id']}" and p["batches"][-1]["label"].startswith("from-email-")
    assert ("m2", inbox_mod.DONE_LABEL) in FakeGmail.labels
    # the second check reads nothing twice
    assert gclient.post("/api/mail/check").json()["check"]["new"] == 0
    assert len(gclient.get(f"/api/projects/{slug}").json()["inbound"]) == 1


def test_labelled_mail_is_placed_by_the_link_inside_or_handed_to_the_engineer(gclient, tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    slug, rev, _ = _finished_review(gclient, tmp_path)
    tok = gclient.post(f"/api/projects/{slug}/reviews/{rev['id']}/share").json()["share"]["id"]
    _connected(gclient)
    # the engineer's own assistant forwarded two emails with the Closeout label: one quotes the contractor's link, one does not
    FakeGmail.arrive(_raw("pm@builder.com", "Fwd: site photos", f"See https://closeout.example.com/c/{tok} for the items. Photo attached.",
                          [("IMG_0031.jpg", _jpeg_bytes())]), labels=("Closeout",))
    FakeGmail.arrive(_raw("pm@builder.com", "Fwd: more photos", "Here is the sprinkler certificate.", [("cert.jpg", _jpeg_bytes())]),
                     labels=("Closeout",))
    FakeGmail.arrive(_raw("someone@else.com", "unrelated", "not for Closeout"))          # no label: never read
    r = gclient.post("/api/mail/check").json()
    assert r["check"] == {"looked_at": 2, "new": 2, "placed": 1, "unplaced": 1}
    _wait_for_run(gclient)
    assert len(r["unplaced"]) == 1 and r["unplaced"][0]["subject"] == "Fwd: more photos" and r["projects"][0]["slug"] == slug
    p = gclient.get(f"/api/projects/{slug}").json()
    assert [x["how"] for x in p["inbound"]] == ["link"]
    # the engineer places the other one; its file is filed to the review they chose
    unplaced_id = r["unplaced"][0]["id"]
    assert gclient.post(f"/api/mail/{unplaced_id}/place", json={"slug": slug, "review_id": "nope"}).status_code == 404
    r2 = gclient.post(f"/api/mail/{unplaced_id}/place", json={"slug": slug, "review_id": rev["id"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["email"]["how"] == "engineer" and r2.json()["unplaced"] == []
    _wait_for_run(gclient)
    p = gclient.get(f"/api/projects/{slug}").json()
    assert sorted(x["how"] for x in p["inbound"]) == ["engineer", "link"] and all(x["status"] == "placed" for x in p["inbound"])
    assert sum(1 for b in p["batches"] if b["via"].startswith("email:")) == 2


def test_without_a_google_client_the_office_page_says_so_and_nothing_else_changes(client, tmp_path):
    j = client.get("/api/mail").json()
    assert j["configured"] is False and j["account"] is None
    assert client.get("/api/mail/connect", follow_redirects=False).status_code == 409
    assert client.post("/api/mail/check").status_code == 409


def test_parse_raw_keeps_the_words_and_the_files():
    inc = gmail_mod.parse_raw(_raw("a@b.com", "Hello", "Two lines\nof text", [("x.jpg", b"\xff\xd8")]), own_address="A@B.com")
    assert inc.from_addr == "a@b.com" and inc.subject == "Hello" and inc.text == "Two lines\nof text" and inc.files == [("x.jpg", b"\xff\xd8")]
    assert inc.from_me is True
