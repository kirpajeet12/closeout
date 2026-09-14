"""The whole mailbox on the Emails page: the office reads its inbox and sent mail, opens a conversation, replies in it and
writes new email. Everything runs against fakes; nothing reaches a real mailbox, and nothing is sent without a press."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from closeout import gmail as gmail_mod, mailbox as mailbox_mod
from tests.test_inbox import FakeImap
from tests.test_review import client  # noqa: F401  (fixture)


def _mail(frm: str, to: str, subject: str, text: str, mid: str, refs: str = "", files=()) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, mid
    m["Date"] = "Sat, 12 Sep 2026 09:15:00 -0700"
    if refs:
        m["In-Reply-To"], m["References"] = refs.split()[-1], refs
    m.set_content(text)
    for name, data in files:
        m.add_attachment(data, maintype="image", subtype="jpeg", filename=name)
    return m.as_bytes()


class BoxImap(FakeImap):
    """The work-mailbox fake with the Emails page's three calls."""
    replies: list[dict] = []
    broken = False

    def list_messages(self, folder="inbox", q="", limit=30):
        if BoxImap.broken:
            raise mailbox_mod.MailError("the email provider refused the address and password")
        out = []
        for uid, m in FakeImap.mailbox.items():
            v = gmail_mod.view_raw(m["raw"], self.address)
            if (folder == "sent") != v["from_me"] or (q and q.lower() not in (v["subject"] + v["text"]).lower()):
                continue
            out.append(gmail_mod.summary(uid, "", v["from"], v["to"], v["subject"], v["text"][:80], v["at"], True, self.address))
        return out

    def open_thread(self, uid):
        return [{**gmail_mod.view_raw(FakeImap.mailbox[uid]["raw"], self.address), "id": uid}]

    def reply(self, uid, body):
        v = gmail_mod.view_raw(FakeImap.mailbox[uid]["raw"], self.address)
        to, subject, headers = gmail_mod.reply_parts(v, self.address)
        BoxImap.replies.append({"to": to, "subject": subject, "body": body, **headers})
        return {"id": "<r1@example.ca>", "thread_id": ""}


def test_the_office_reads_replies_to_and_writes_email_from_its_own_mailbox(client, monkeypatch):
    FakeImap.mailbox, FakeImap.sent, BoxImap.replies, BoxImap.broken = {}, [], [], False
    monkeypatch.setattr(mailbox_mod, "ImapMail", BoxImap)
    # nothing connected: the page is told plainly
    none = client.get("/api/mail/box")
    assert none.status_code == 409 and "no mailbox" in none.json()["detail"]
    assert client.post("/api/mail/box/send", json={"to": "a@b.com", "subject": "x", "body": "y"}).status_code == 409
    assert client.post("/api/mail/email", json={"address": "reviews@office-example.ca", "password": FakeImap.password,
                                                "host": "hostinger"}).status_code == 200
    FakeImap.arrive(_mail("Site Super <site@contractor.com>", "reviews@office-example.ca", "Gate code", "The gate code is 4411.", "<in1@contractor.com>"))
    FakeImap.arrive(_mail("reviews@office-example.ca", "pm@builder.com", "Schedule", "Walk on Friday.", "<out1@office-example.ca>"))
    inbox = client.get("/api/mail/box").json()
    assert inbox["address"] == "reviews@office-example.ca"
    assert [m["subject"] for m in inbox["messages"]] == ["Gate code"] and inbox["messages"][0]["from"] == "Site Super"
    assert [m["subject"] for m in client.get("/api/mail/box?folder=sent").json()["messages"]] == ["Schedule"]
    assert client.get("/api/mail/box?q=gate").json()["messages"] and not client.get("/api/mail/box?q=nothing").json()["messages"]
    uid = inbox["messages"][0]["id"]
    thread = client.get("/api/mail/box/open", params={"id": uid}).json()["messages"]
    assert thread[0]["text"] == "The gate code is 4411." and thread[0]["from_me"] is False
    # a reply goes back to the sender in the same conversation, only on the press
    assert client.post("/api/mail/box/send", json={"reply_to": uid, "body": "  "}).status_code == 400 and not BoxImap.replies
    ok = client.post("/api/mail/box/send", json={"reply_to": uid, "body": "Thanks, see you Friday."})
    assert ok.status_code == 200, ok.text
    assert BoxImap.replies == [{"to": "Site Super <site@contractor.com>", "subject": "Re: Gate code", "body": "Thanks, see you Friday.",
                                "In-Reply-To": "<in1@contractor.com>", "References": "<in1@contractor.com>"}]
    # a new email needs an address and a subject
    assert client.post("/api/mail/box/send", json={"to": "not an address", "subject": "Hi", "body": "x"}).status_code == 400
    assert client.post("/api/mail/box/send", json={"to": "pm@builder.com", "subject": " ", "body": "x"}).status_code == 400
    new = client.post("/api/mail/box/send", json={"to": "pm@builder.com", "subject": "Level 2 walk", "body": "Tuesday at 9."})
    assert new.status_code == 200 and FakeImap.sent[-1] == {"to": "pm@builder.com", "subject": "Level 2 walk", "body": "Tuesday at 9.", "id": "<sent1@example.ca>"}
    # the provider refusing is a sentence, never the password
    BoxImap.broken = True
    bad = client.get("/api/mail/box")
    assert bad.status_code == 502 and "refused" in bad.json()["detail"] and FakeImap.password not in bad.text


def test_a_reply_to_a_message_the_office_sent_goes_to_the_same_people_and_keeps_the_chain():
    raw = _mail("reviews@office-example.ca", "pm@builder.com", "Re: Schedule", "Friday.", "<b@office-example.ca>", refs="<a@builder.com>")
    to, subject, headers = gmail_mod.reply_parts(gmail_mod.view_raw(raw, "reviews@office-example.ca"), "reviews@office-example.ca")
    assert to == "pm@builder.com" and subject == "Re: Schedule"
    assert headers == {"In-Reply-To": "<b@office-example.ca>", "References": "<a@builder.com> <b@office-example.ca>"}
    msg = gmail_mod.build_message("reviews@office-example.ca", to, subject, "ok", headers=headers)
    assert msg["In-Reply-To"] == "<b@office-example.ca>" and msg["References"] == "<a@builder.com> <b@office-example.ca>"


def test_gmail_reply_stays_in_the_thread(monkeypatch):
    raw = _mail("Site Super <site@contractor.com>", "office@example.com", "Photos", "Attached.", "<p1@contractor.com>",
                files=[("IMG_1.jpg", b"\xff\xd8")])
    posted = []

    def fake_get(url, headers=None, params=None, timeout=None):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"id": "m1", "threadId": "t1", "raw": base64.urlsafe_b64encode(raw).decode()}
        return R()

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append((url, json))
        class R:
            def raise_for_status(self): pass
            def json(self): return {"id": "m2", "threadId": "t1"}
        return R()

    monkeypatch.setattr(gmail_mod.httpx, "get", fake_get)
    monkeypatch.setattr(gmail_mod.httpx, "post", fake_post)
    g = gmail_mod.Gmail(None, "refresh", "office@example.com")
    g._token = "t"
    assert g.reply("m1", "Got them, thanks.") == {"id": "m2", "thread_id": "t1"}
    url, body = posted[0]
    assert url.endswith("messages/send") and body["threadId"] == "t1"
    sent = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "In-Reply-To: <p1@contractor.com>" in sent and "Subject: Re: Photos" in sent and "To: Site Super <site@contractor.com>" in sent


class FakeImapServer:
    """Just enough of imaplib's answers for the list: two headers with flags, one read and one unread."""

    def __init__(self):
        self.selected = []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent Items"']

    def select(self, name, readonly=False):
        self.selected.append(name)
        return "OK", [b"2"]

    def response(self, code):
        return code, [b"9"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"3 4"]
        head = lambda s, f: f"From: Site <site@c.com>\r\nTo: office@example.com\r\nSubject: {s}\r\nDate: {f}\r\n\r\n".encode()
        return "OK", [(b"1 (UID 3 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {90}", head("Old", "Fri, 11 Sep 2026 09:00:00 -0700")), b")",
                      (b"2 (UID 4 FLAGS () BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {90}", head("New", "Sat, 12 Sep 2026 09:00:00 -0700")), b")"]


def test_a_work_mailbox_lists_newest_first_with_unread_marked_and_the_inbox_selected_again():
    box = mailbox_mod.ImapMail("office@example.com", "pw", "imap.example.com", 993, "smtp.example.com", 465)
    server = FakeImapServer()
    box._imap, box._validity = server, "9"
    got = box.list_messages("sent")
    assert [(m["subject"], m["unread"], m["id"]) for m in got] == [("New", True, "sent:9:4"), ("Old", False, "sent:9:3")]
    assert server.selected == ['"Sent Items"', "INBOX"]
