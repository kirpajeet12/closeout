"""Sending the covering message by email: only on the engineer's press, with the review's reference in the subject so replies come home."""

from __future__ import annotations

from closeout import inbox as inbox_mod, mail as mail_mod
from closeout.store import Store
from tests.test_review import FakeFieldAgent, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


class FakeSES:
    calls: list[dict] = []
    fail = False

    def __init__(self, *a, **k):
        pass

    def send_email(self, **kw):
        if FakeSES.fail:
            raise RuntimeError("MessageRejected: address not verified")
        FakeSES.calls.append(kw)
        return {"MessageId": f"msg-{len(FakeSES.calls)}"}


def test_the_message_needs_an_address_and_carries_the_review_reference_not_a_link(client, tmp_path):
    slug, rev, _ = _finished_review(client, tmp_path)
    url = f"/api/projects/{slug}/reviews/{rev['id']}/send"
    assert client.post(url, json={"to": "not an address"}).status_code == 400
    assert client.post(f"/api/projects/{slug}/reviews/nope/send", json={"to": "site@contractor.com"}).status_code == 404
    assert client.get(f"/api/projects/{slug}").json()["sends"] == []                        # nothing recorded
    r = client.post(url, json={"to": "site@contractor.com"})                                 # no link needed: the reply is by email
    assert r.status_code == 200, r.text
    s = r.json()["send"]
    assert "/c/" not in s["body"] and s["subject"].endswith(f"[{inbox_mod.review_ref(rev['id'])}]")


def test_without_a_sender_the_mail_app_sends_and_the_app_only_keeps_the_record(client, tmp_path):
    slug, rev, _ = _finished_review(client, tmp_path)
    assert client.settings.mail_from == ""
    j = client.get(f"/api/projects/{slug}").json()
    assert j["mail"]["from"] == "" and j["mail"]["gmail"] == ""
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": " Site@Contractor.com "})
    assert r.status_code == 200, r.text
    s = r.json()["send"]
    assert s["via"] == "mail-app" and s["to_addr"] == "Site@Contractor.com" and "/c/" not in s["body"] and s["message_id"] == ""
    assert s["subject"].startswith("Field review 1 (Electrical)")
    assert [x["id"] for x in client.get(f"/api/projects/{slug}").json()["sends"]] == [s["id"]]
    # the record is what the Ask panel reads
    st = Store(client.settings.data_dir / "closeout.db")
    assert st.sends(st.project_by_slug(slug)["id"])[0]["draft_id"]


def test_with_a_verified_sender_the_app_sends_through_ses_on_the_press(client, tmp_path, monkeypatch):
    import dataclasses
    from fastapi.testclient import TestClient
    from closeout import api
    slug, rev, _ = _finished_review(client, tmp_path)
    client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share")
    # the same data, served by an app whose office has a verified sender
    client = TestClient(api.create_app(dataclasses.replace(client.settings, mail_from="reviews@example.com")))
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSES())
    FakeSES.calls, FakeSES.fail = [], True
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com"})
    assert r.status_code == 502 and client.get(f"/api/projects/{slug}").json()["sends"] == []     # refused: nothing recorded
    FakeSES.fail = False
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com"})
    assert r.status_code == 200, r.text
    s = r.json()["send"]
    assert s["via"] == "ses" and s["message_id"] == "msg-1"
    call = FakeSES.calls[0]
    assert call["FromEmailAddress"] == "reviews@example.com" and call["Destination"] == {"ToAddresses": ["site@contractor.com"]}
    assert call["ReplyToAddresses"] == ["reviews@example.com"]
    import email as email_lib
    from email import policy
    raw = email_lib.message_from_bytes(call["Content"]["Raw"]["Data"], policy=policy.default)
    assert raw["Subject"] == s["subject"] and "/c/" in raw.get_body(("plain",)).get_content()
    pdfs = [part for part in raw.iter_attachments() if part.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1 and pdfs[0].get_filename() == s["report"]           # the items report goes with it
    # the engineer chose the mail app anyway: recorded as such, nothing sent by the app
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com", "via": "mail-app"})
    assert r.status_code == 200 and r.json()["send"]["via"] == "mail-app" and len(FakeSES.calls) == 1
    assert mail_mod.valid_address("a@b.co") and not mail_mod.valid_address("a@b")
