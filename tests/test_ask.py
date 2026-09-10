"""Ask Closeout, layer one: a question answered from the records, with an optional move of the screen. Reads only."""
from __future__ import annotations

import pytest

from closeout import ask as ask_mod
from closeout.store import Store
from tests.test_review import FakeFieldAgent, client  # noqa: F401  (fixture)
from tests.test_share import _finished_review


class FakeAskAgent:
    """Stands in for strands.Agent: calls the answer tool with canned arguments, first one that is accepted wins."""
    answers: list[dict] = []
    proposals: list[dict] = []
    calls: list = []
    lookups: list[str] = []
    replies: list[str] = []

    def __init__(self, model=None, tools=None, system_prompt="", callback_handler=None):
        self.tools = {t.tool_name if hasattr(t, "tool_name") else getattr(t, "__name__", "tool"): t for t in tools}
        self.system_prompt = system_prompt

    def __call__(self, content):
        FakeAskAgent.calls.append(content)
        for name, t in self.tools.items():
            if "list_items" in name:
                FakeAskAgent.lookups.append(t())
        propose = next(t for name, t in self.tools.items() if "propose" in name)
        for args in FakeAskAgent.proposals:
            FakeAskAgent.replies.append(propose(**args))
        answer = next(t for name, t in self.tools.items() if "answer" in name)
        for args in FakeAskAgent.answers:
            if answer(**args) == "recorded":
                break

        class R:
            class metrics:
                accumulated_usage = {"inputTokens": 500, "outputTokens": 60}
        return R()


@pytest.fixture
def asking(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_mod, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(ask_mod, "Agent", FakeAskAgent)
    FakeAskAgent.answers, FakeAskAgent.calls, FakeAskAgent.lookups = [], [], []
    FakeAskAgent.proposals, FakeAskAgent.replies = [], []
    return client


def test_answer_moves_the_screen_only_to_places_that_exist(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.answers = [
        {"text": "One item is open at 9999.", "go_screen": "deficiencies", "building": "9999"},          # no such building
        {"text": "Open it.", "go_screen": "item", "item_id": "EL-77"},                                      # no such item
        {"text": "Open it.", "go_screen": "somewhere"},                                                     # no such screen
        {"text": "EL-01 is the one item recorded in Field review 1; nothing has been received for it yet.",
         "go_screen": "deficiencies", "review": "Field review 1"},
    ]
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "what is open from the first review?", "where": {"tab": "overview"}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["answer"].startswith("EL-01 is the one item")
    assert j["go"] == {"screen": "deficiencies", "review": rev["id"], "discipline": "EL"}
    # the fake looked the items up and the record names the item with its state
    assert FakeAskAgent.lookups and "EL-01" in FakeAskAgent.lookups[0] and "nothing received yet" in FakeAskAgent.lookups[0]
    # the facts and the engineer's screen went in with the question
    sent = " ".join(b["text"] for b in FakeAskAgent.calls[0])
    assert "Row Houses" in sent and "screen: overview" in sent and "QUESTION: what is open" in sent
    # one run of kind ask, and nothing written to the records
    st = Store(asking.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    runs = st.runs(pid, kind="ask")
    assert len(runs) == 1 and runs[0]["status"] == "done"
    assert len(st.deficiencies(pid)) == 1 and len(st.all_drafts(pid)) == 1


def test_judging_words_are_refused_and_the_plain_answer_has_no_go(asking, tmp_path):
    slug, _, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.answers = [
        {"text": "EL-01 is compliant and approved."},
        {"text": "Nothing has been received for EL-01; it still needs a photo of the completed work."},
    ]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "is EL-01 done?"}).json()
    assert j["answer"].startswith("Nothing has been received") and j["go"] is None


def test_empty_question_and_no_answer_are_errors(asking, tmp_path):
    slug, _, _ = _finished_review(asking, tmp_path)
    assert asking.post(f"/api/projects/{slug}/ask", json={"question": "   "}).status_code == 400
    FakeAskAgent.answers = [{"text": ""}]
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "hello?"})
    assert r.status_code == 502 and "ask again" in r.json()["detail"]
    st = Store(asking.settings.data_dir / "closeout.db")
    runs = st.runs(st.project_by_slug(slug)["id"], kind="ask")
    assert [x["status"] for x in runs] == ["failed", "failed"]


def test_item_and_sheet_screens_resolve_to_ids(asking, tmp_path):
    slug, _, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.answers = [{"text": "Here is EL-01.", "go_screen": "item", "item_id": "el-01"}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "open el-01"}).json()
    assert j["go"] == {"screen": "item", "item_id": "EL-01"}
    FakeAskAgent.answers = [{"text": "Here is the upper floor power plan.", "go_screen": "sheet", "sheet": "EL-2"}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "show me EL-2"}).json()
    assert j["go"]["screen"] == "sheet" and j["go"]["sheet"] == "EL-2" and j["go"]["sheet_id"].startswith("sh")


def test_a_change_is_prepared_but_not_made_until_the_engineer_confirms(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.proposals = [
        {"kind": "decide", "item_id": "EL-77", "decision": "accept"},                 # no such item
        {"kind": "decide", "item_id": "el-01", "decision": "maybe"},                  # no such decision
        {"kind": "decide", "item_id": "el-01", "decision": "hold", "note": "waiting on the photo"},
    ]
    FakeAskAgent.answers = [{"text": "Ready to confirm: EL-01 on hold with your note."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "put EL-01 on hold, waiting on the photo"}).json()
    a = j["action"]
    assert a["kind"] == "decide" and a["method"] == "POST" and a["path"] == "/items/EL-01/decision"
    assert a["body"] == {"decision": "hold", "note": "waiting on the photo"} and a["then"] == {"screen": "item", "item_id": "EL-01"}
    assert a["label"] == 'Mark EL-01 as on hold, with the note "waiting on the photo"'
    assert j["go"] is None and FakeAskAgent.replies[0].startswith("REJECTED") and FakeAskAgent.replies[1].startswith("REJECTED")
    st = Store(asking.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    assert st.decisions(pid) == []                                   # nothing changed yet
    r = asking.post(f"/api/projects/{slug}{a['path']}", json=a["body"])   # the screen's Confirm button does exactly this
    assert r.status_code == 200 and st.decisions(pid)[0]["decision"] == "hold"


def test_review_and_link_changes_respect_the_state_of_the_review(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    # finished review: cannot be finished again, can get a link; no active electrical review: one can start
    FakeAskAgent.proposals = [{"kind": "finish_review", "review": "Field review 1"}, {"kind": "create_link", "review": "field review 1"}]
    FakeAskAgent.answers = [{"text": "Ready to confirm."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "send field review 1 to the contractor"}).json()
    assert FakeAskAgent.replies[0].startswith("REJECTED") and "already finished" in FakeAskAgent.replies[0]
    assert j["action"]["kind"] == "create_link" and j["action"]["path"] == f"/reviews/{rev['id']}/share"
    asking.post(f"/api/projects/{slug}/reviews/{rev['id']}/share")
    FakeAskAgent.proposals, FakeAskAgent.replies = [{"kind": "create_link", "review": "Field review 1"}, {"kind": "turn_off_link", "review": "Field review 1"}], []
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "turn the link off"}).json()
    assert "already has an active" in FakeAskAgent.replies[0]
    assert j["action"]["kind"] == "turn_off_link" and j["action"]["method"] == "DELETE" and j["action"]["path"].startswith("/shares/")
    FakeAskAgent.proposals, FakeAskAgent.replies = [{"kind": "start_review", "discipline": "xx"}, {"kind": "start_review", "discipline": "el"}], []
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "start an electrical review"}).json()
    assert FakeAskAgent.replies[0].startswith("REJECTED") and j["action"]["body"] == {"discipline": "EL"} and j["action"]["then"] == {"screen": "field", "discipline": "EL"}
    asking.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"})
    FakeAskAgent.proposals, FakeAskAgent.replies = [{"kind": "start_review", "discipline": "EL"}], []
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "start another"}).json()
    assert "already in progress" in FakeAskAgent.replies[0] and j["action"] is None


def test_edit_item_changes_only_the_named_fields(asking, tmp_path):
    slug, _, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.proposals = [{"kind": "edit_item", "item_id": "EL-01"}, {"kind": "edit_item", "item_id": "EL-01", "description": "Receptacle beside the basin has no cover plate; plate missing entirely."}]
    FakeAskAgent.answers = [{"text": "Ready to confirm the new wording on EL-01."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "change EL-01 wording"}).json()
    a = j["action"]
    assert FakeAskAgent.replies[0].startswith("REJECTED")
    assert a["method"] == "PATCH" and a["path"] == "/findings/EL-01" and list(a["body"]) == ["description"] and a["label"] == "Change the wording on EL-01"
    r = asking.patch(f"/api/projects/{slug}{a['path']}", json=a["body"])
    assert r.status_code == 200
    st = Store(asking.settings.data_dir / "closeout.db")
    d = st.deficiency(st.project_by_slug(slug)["id"], "EL-01")
    assert d["description"].endswith("plate missing entirely.") and d["location"].startswith("Unit C")


def test_a_spoken_follow_up_carries_the_conversation_and_asks_for_a_short_answer(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    cl, pid = asking, slug
    FakeAskAgent.answers = [{"text": "Two are still open on the second floor."}]
    history = [{"q": "what is open at the row houses?", "a": "Three items are open."}, {"q": "", "a": "ignored"}]
    r = cl.post(f"/api/projects/{pid}/ask", json={"question": "and the second floor?", "history": history, "spoken": True})
    assert r.status_code == 200 and r.json()["answer"] == "Two are still open on the second floor."
    sent = "\n".join(b["text"] for b in FakeAskAgent.calls[-1])
    assert "EARLIER IN THIS CONVERSATION" in sent and "Engineer: what is open at the row houses?" in sent
    assert "Closeout: Three items are open." in sent and "ignored" not in sent
    assert "SPOKEN:" in sent and sent.index("SPOKEN:") < sent.index("QUESTION: and the second floor?")
    r = cl.post(f"/api/projects/{pid}/ask", json={"question": "and the second floor?"})
    sent = "\n".join(b["text"] for b in FakeAskAgent.calls[-1])
    assert "SPOKEN:" not in sent and "EARLIER" not in sent
