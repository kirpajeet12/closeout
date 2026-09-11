"""Conversations: every question and answer is kept on the project and can be picked up later."""
from __future__ import annotations

from tests.test_ask import FakeAskAgent, asking  # noqa: F401  (fixture)
from tests.test_review import client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


def test_a_question_starts_a_saved_conversation_and_a_follow_up_continues_it(asking, tmp_path):
    slug = _finished_review(asking, tmp_path)[0]
    assert asking.get(f"/api/projects/{slug}/conversations").json()["conversations"] == []

    FakeAskAgent.answers = [{"text": "Two items are still open at the row houses."}]
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "what is still open?"})
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    assert cid.startswith("conv_") and r.json()["conversation_title"] == "what is still open?"

    lst = asking.get(f"/api/projects/{slug}/conversations").json()["conversations"]
    assert [c["id"] for c in lst] == [cid] and lst[0]["turns"] == 1 and lst[0]["last_answer"].startswith("Two items")

    FakeAskAgent.answers = [{"text": "Both are electrical."}]
    FakeAskAgent.calls = []
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "which discipline?", "conversation_id": cid, "spoken": True})
    assert r.status_code == 200, r.text
    assert r.json()["conversation_id"] == cid
    # the earlier turn is carried into the model call as history
    assert "what is still open?" in str(FakeAskAgent.calls[-1]) and "Two items" in str(FakeAskAgent.calls[-1])

    conv = asking.get(f"/api/projects/{slug}/conversations/{cid}").json()
    assert [t["q"] for t in conv["turns"]] == ["what is still open?", "which discipline?"]
    assert conv["turns"][1]["spoken"] is True and conv["turns"][0]["spoken"] is False


def test_conversations_can_be_started_named_renamed_and_deleted(asking, tmp_path):
    slug = _finished_review(asking, tmp_path)[0]
    c = asking.post(f"/api/projects/{slug}/conversations", json={"title": "Walk on the 12th"}).json()
    assert c["title"] == "Walk on the 12th" and c["turns"] == 0
    r = asking.patch(f"/api/projects/{slug}/conversations/{c['id']}", json={"title": "  "})
    assert r.status_code == 400
    r = asking.patch(f"/api/projects/{slug}/conversations/{c['id']}", json={"title": "Second walk"})
    assert r.json()["title"] == "Second walk"
    assert asking.get(f"/api/projects/{slug}/conversations/conv_nope").status_code == 404
    assert asking.post(f"/api/projects/{slug}/ask", json={"question": "hi", "conversation_id": "conv_nope"}).status_code == 404
    assert asking.delete(f"/api/projects/{slug}/conversations/{c['id']}").json() == {"deleted": c["id"]}
    assert asking.get(f"/api/projects/{slug}/conversations").json()["conversations"] == []
