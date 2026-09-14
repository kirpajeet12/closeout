"""After a field review is finished: the office says who gets the report, the draft keeps that address, and Send uses it.
Nothing leaves without the press; the address survives saving, sending and writing the email again."""
from __future__ import annotations

from tests.test_review import FakeFieldAgent, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


def _draft(client, slug, rid):
    return next(m for m in client.get(f"/api/projects/{slug}").json()["messages"] if m["review_id"] == rid)


def test_the_address_given_on_finish_is_kept_with_the_draft_and_nothing_is_sent(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    assert msg["to_addr"] == ""
    url = f"/api/projects/{slug}/reviews/{rev['id']}/finish"
    assert client.post(url, json={"to": "not an address"}).status_code == 400
    out = client.post(url, json={"to": " site@contractor.com "}).json()
    assert out["message"]["id"] == msg["id"] and out["message"]["to_addr"] == "site@contractor.com"
    p = client.get(f"/api/projects/{slug}").json()
    assert _draft(client, slug, rev["id"])["to_addr"] == "site@contractor.com" and p["sends"] == []


def test_saving_the_draft_keeps_the_address_and_the_wording(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    assert client.patch(f"/api/drafts/{msg['id']}", json={"to": "nope"}).status_code == 400
    r = client.patch(f"/api/drafts/{msg['id']}", json={"to": "pm@builder.com", "subject": "EL items", "body": "Please see the report."})
    assert r.status_code == 200, r.text
    d = _draft(client, slug, rev["id"])
    assert (d["to_addr"], d["subject"], d["body"]) == ("pm@builder.com", "EL items", "Please see the report.")
    client.patch(f"/api/drafts/{msg['id']}", json={"body": "Changed only the words."})
    assert _draft(client, slug, rev["id"])["to_addr"] == "pm@builder.com"


def test_sending_remembers_who_it_went_to_even_when_the_email_is_written_again(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/send", json={"to": "site@contractor.com", "subject": "EL items", "body": "See the report."})
    assert r.status_code == 200, r.text
    assert r.json()["send"]["to_addr"] == "site@contractor.com"
    assert _draft(client, slug, rev["id"])["to_addr"] == "site@contractor.com"
    FakeFieldAgent.messages = [{"subject": "Field review 1 (Electrical): written again", "body": msg["body"]}]
    again = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/message").json()["message"]
    assert again["id"] != msg["id"] and again["to_addr"] == "site@contractor.com"
