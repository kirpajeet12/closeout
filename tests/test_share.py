"""The contractor's link: one per finished field review. They open it, see the items, send evidence back through it.
The office never sends anything from the app; the link goes into the covering message for the engineer to copy."""
from __future__ import annotations

import time

from closeout import pipeline
from closeout.store import Store
from tests.test_api import FakeAgent
from tests.test_review import FakeFieldAgent, _jpeg_bytes, _seed, client  # noqa: F401  (fixture)


def _finished_review(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": 0.3, "pin_y": 0.4, "review_id": rev["id"],
                "location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": "Receptacle beside the basin has no cover plate.",
                "evidence_required": "photo: completed", "unit": "Unit C", "level": "Upper Floor"},
                files={"photo": ("IMG_0002.jpg", _jpeg_bytes(), "image/jpeg")})
    body = ("Please find below the items recorded during Field review 1 (Electrical).\n\n"
            "EL-01 · Unit C, Upper Floor, Bath 2: wall behind toilet\nReceptacle beside the basin has no cover plate.\nSend to close: photo: completed\n\n"
            "Please reply with the evidence named above by [date].\n\nRow Houses site office")
    FakeFieldAgent.messages = [{"subject": "Field review 1 (Electrical): 1 item to close at Row Houses", "body": body}]
    out = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish").json()
    assert out["error"] is None and out["message"]
    return slug, rev, out["message"]


def test_link_needs_a_finished_review_and_lands_in_the_message(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    active = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    assert client.post(f"/api/projects/{slug}/reviews/{active['id']}/share").status_code == 409     # still active
    assert client.post(f"/api/projects/{slug}/reviews/nope/share").status_code == 404
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share")
    assert r.status_code == 200, r.text
    tok, url = r.json()["share"]["id"], r.json()["url"]
    assert url.endswith("/c/" + tok) and len(tok) >= 24
    assert r.json()["message"]["body"].endswith("through this page: " + url)
    # asking again gives the same link and does not add the line twice
    again = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share").json()
    assert again["share"]["id"] == tok and again["message"]["body"].count("/c/") == 1
    p = client.get(f"/api/projects/{slug}").json()
    assert p["shares"][0]["id"] == tok and p["messages"][0]["body"].count(url) == 1
    assert p["card"]["links"] == 1 and p["card"]["reviews_finished"] == 1 and p["card"]["messages"] == 1
    assert client.get("/api/projects").json()["projects"][0]["links"] == 1


def test_contractor_sees_only_their_package_and_the_page_is_served(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    tok = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share").json()["share"]["id"]
    v = client.get(f"/api/c/{tok}").json()
    assert v["active"] and v["project"] == {"name": "Row Houses"} and v["review"]["count"] == 1
    assert v["items"][0]["item"]["item_id"] == "EL-01" and v["items"][0]["completeness"] == "no_evidence"
    assert set(v) == {"active", "project", "office", "review", "items", "drops", "busy"}     # no decisions, drafts, register
    assert "address" not in v["project"]
    assert client.get("/api/c/not-a-link").status_code == 404
    page = client.get(f"/c/{tok}")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]


def test_contractor_upload_is_filed_as_a_drop_from_them(client, tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(pipeline, "run_match_job", fake.match)
    monkeypatch.setattr(pipeline, "run_draft_job", fake.draft)
    slug, rev, msg = _finished_review(client, tmp_path)
    tok = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share").json()["share"]["id"]
    r = client.post(f"/api/c/{tok}/batches", files=[("files", ("IMG_0009.jpg", _jpeg_bytes(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    assert r.json()["label"].startswith("from-contractor-") and r.json()["files"] == 1
    for _ in range(100):
        runs = Store(client.settings.data_dir / "closeout.db").runs(kind="batch")
        if runs and runs[-1]["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    v = client.get(f"/api/c/{tok}").json()
    assert len(v["drops"]) == 1 and v["drops"][0]["files"] == 1 and v["drops"][0]["status"] == "done"
    p = client.get(f"/api/projects/{slug}").json()
    assert p["batches"][-1]["via"] == tok and p["card"]["from_contractor"] == 1 and p["card"]["drops"] == 1
    # turned off: the page says so, uploads stop
    assert client.delete(f"/api/projects/{slug}/shares/{tok}").json()["share"]["revoked_at"]
    assert client.get(f"/api/c/{tok}").json()["active"] is False
    assert client.post(f"/api/c/{tok}/batches", files=[("files", ("IMG_0010.jpg", _jpeg_bytes(), "image/jpeg"))]).status_code == 410
    assert client.get(f"/api/projects/{slug}").json()["card"]["links"] == 0


def test_contractor_sees_the_offices_call_but_not_its_note(client, tmp_path):
    slug, rev, msg = _finished_review(client, tmp_path)
    tok = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/share").json()["share"]["id"]
    item = lambda: client.get(f"/api/c/{tok}").json()["items"][0]
    assert item()["call"] is None and not item()["closed"]
    for decision, call, closed in (("reject", "reject", False), ("hold", "hold", False), ("accept", None, True)):
        r = client.post(f"/api/projects/{slug}/items/EL-01/decision", json={"decision": decision, "note": "office only: photo too dark"})
        assert r.status_code == 200, r.text
        assert item()["call"] == call and item()["closed"] is closed
    assert "office only" not in client.get(f"/api/c/{tok}").text
