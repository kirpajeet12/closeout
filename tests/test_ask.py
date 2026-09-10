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
    calls: list = []
    lookups: list[str] = []

    def __init__(self, model=None, tools=None, system_prompt="", callback_handler=None):
        self.tools = {t.tool_name if hasattr(t, "tool_name") else getattr(t, "__name__", "tool"): t for t in tools}
        self.system_prompt = system_prompt

    def __call__(self, content):
        FakeAskAgent.calls.append(content)
        for name, t in self.tools.items():
            if "list_items" in name:
                FakeAskAgent.lookups.append(t())
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
