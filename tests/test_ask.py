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

            def __str__(self):
                return FakeAskAgent.plain
        return R()

    plain: str = ""


@pytest.fixture
def asking(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_mod, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(ask_mod, "Agent", FakeAskAgent)
    FakeAskAgent.answers, FakeAskAgent.calls, FakeAskAgent.lookups = [], [], []
    FakeAskAgent.proposals, FakeAskAgent.replies = [], []
    FakeAskAgent.plain = ""
    return client


def test_a_plain_text_reply_still_answers_the_question(asking, tmp_path):
    """Now and then the model answers in text without calling the answer tool; the engineer still gets that text."""
    slug, _, _ = _finished_review(asking, tmp_path)
    FakeAskAgent.answers = []
    FakeAskAgent.plain = "EL-01 is still open; nothing has been received for it.\n"
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "what is open?", "where": {"tab": "overview"}})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "EL-01 is still open; nothing has been received for it." and r.json()["go"] is None
    # with no text at all the question fails cleanly, and the run says so
    FakeAskAgent.plain = ""
    r = asking.post(f"/api/projects/{slug}/ask", json={"question": "what is open?", "where": {"tab": "overview"}})
    assert r.status_code == 502


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


def test_a_spoken_move_or_rename_is_prepared_for_the_confirm_step(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    st = Store(asking.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    st.replace_documents(pid, [{"rel_path": "PM/Sprinkler test cert.pdf", "discipline": "PM", "dated": None, "pages": 1, "kind": "document",
                                "is_current": 0, "sha256": "x", "size": 1}])
    FakeAskAgent.proposals = [
        {"kind": "file_document", "file": "nothing.pdf", "discipline": "EL"},                # no such file
        {"kind": "file_document", "file": "sprinkler"},                                      # nothing to change
        {"kind": "file_document", "file": "sprinkler", "building": "9999 Nowhere"},          # no such building
        {"kind": "file_document", "file": "sprinkler", "discipline": "electrical", "name": "Sprinkler material test certificate, above ground"},
    ]
    FakeAskAgent.answers = [{"text": "Ready to confirm: the sprinkler certificate goes under Electrical with its new name."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "put the sprinkler file under electrical and call it the above ground test certificate"}).json()
    a = j["action"]
    assert [r.startswith("REJECTED") for r in FakeAskAgent.replies] == [True, True, True, False]
    assert a["kind"] == "file_document" and a["method"] == "POST" and a["path"] == "/filing" and a["then"] == {"screen": "docs"}
    assert a["body"] == {"file": "Sprinkler test cert.pdf", "discipline": "EL", "name": "Sprinkler material test certificate, above ground"}
    assert a["label"].startswith("File Sprinkler test cert.pdf under Electrical shown as")
    assert st.filings(pid) == {}                                                       # nothing moved yet
    r = asking.post(f"/api/projects/{slug}{a['path']}", json=a["body"])                # the Confirm button
    assert r.status_code == 200 and st.filings(pid)["Sprinkler test cert.pdf"]["who"] == "engineer"


def test_a_spoken_new_discipline_folder_is_prepared_for_the_confirm_step(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    st = Store(asking.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    pv = asking.get(f"/api/projects/{slug}").json()["project"]
    before = [d["code"] for d in pv["disciplines"]]
    taken = (before or [s["discipline"] for s in pv["sheets"]])[0]
    FakeAskAgent.proposals = [
        {"kind": "add_discipline", "discipline": taken},                     # already a folder
        {"kind": "add_discipline", "discipline": "ZZ"},                      # unknown code, no name
        {"kind": "add_discipline", "discipline": "sprinkler"[:2]},           # SP: the office knows the name
    ]
    FakeAskAgent.answers = [{"text": "Ready to confirm: a Sprinkler folder is added to the project."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "create 1 more discipline sprinkler for this project"}).json()
    a = j["action"]
    assert [r.startswith("REJECTED") for r in FakeAskAgent.replies] == [True, True, False]
    assert a["kind"] == "add_discipline" and a["method"] == "POST" and a["path"] == "/disciplines" and a["then"] == {"screen": "docs", "folder": ["prj", "site", "site/SP"]}
    assert a["body"] == {"code": "SP", "name": "Sprinkler"} and a["label"] == "Add a Sprinkler folder (SP) to the project"
    assert [d["code"] for d in asking.get(f"/api/projects/{slug}").json()["project"]["disciplines"]] == before   # nothing added yet
    r = asking.post(f"/api/projects/{slug}{a['path']}", json=a["body"])                # the Confirm button
    assert r.status_code == 200
    assert [d["code"] for d in asking.get(f"/api/projects/{slug}").json()["project"]["disciplines"]] == before + ["SP"]


def test_a_folder_the_engineer_added_can_be_renamed_or_removed_through_the_confirm_step(asking, tmp_path):
    slug, rev, _ = _finished_review(asking, tmp_path)
    pv = asking.get(f"/api/projects/{slug}").json()["project"]
    drawn = (pv["disciplines"] or [{"code": s["discipline"]} for s in pv["sheets"]])[0]["code"]
    assert asking.post(f"/api/projects/{slug}/disciplines", json={"code": "SP", "name": "Sprinkler"}).status_code == 200
    assert asking.post(f"/api/projects/{slug}/disciplines", json={"code": "SK", "name": "Sprinkler"}).status_code == 200
    FakeAskAgent.proposals = [
        {"kind": "rename_discipline", "discipline": drawn, "name": "Anything"},   # came with the drawings
        {"kind": "rename_discipline", "discipline": "ZZ", "name": "Sump Pump"},     # no such folder
        {"kind": "rename_discipline", "discipline": "SP", "name": ""},              # no new name
        {"kind": "rename_discipline", "discipline": "sp", "name": "Sump Pump"},
    ]
    FakeAskAgent.answers = [{"text": "Ready to confirm: SP is renamed to Sump Pump."}]
    j = asking.post(f"/api/projects/{slug}/ask", json={"question": "rename the sp folder to sump pump"}).json()
    a = j["action"]
    assert [r.startswith("REJECTED") for r in FakeAskAgent.replies] == [True, True, True, False]
    assert "came with the drawings" in FakeAskAgent.replies[0]
    assert a["kind"] == "rename_discipline" and a["method"] == "PATCH" and a["path"] == "/disciplines/SP" and a["body"] == {"name": "Sump Pump"}
    assert a["label"] == "Rename the SP folder from Sprinkler to Sump Pump"
    names = lambda: {d["code"]: d["name"] for d in asking.get(f"/api/projects/{slug}").json()["project"]["disciplines"]}
    assert names()["SP"] == "Sprinkler"                                                  # nothing changed yet
    assert asking.patch(f"/api/projects/{slug}{a['path']}", json=a["body"]).status_code == 200   # the Confirm button
    assert names()["SP"] == "Sump Pump" and names()["SK"] == "Sprinkler"
    assert asking.patch(f"/api/projects/{slug}/disciplines/{drawn}", json={"name": "Anything"}).status_code == 400

    FakeAskAgent.replies = []
    FakeAskAgent.proposals = [{"kind": "remove_discipline", "discipline": "Sprinkler"}]    # by its name
    FakeAskAgent.answers = [{"text": "Ready to confirm: the empty SK folder is removed."}]
    a = asking.post(f"/api/projects/{slug}/ask", json={"question": "delete the other sprinkler folder"}).json()["action"]
    assert a["kind"] == "remove_discipline" and a["method"] == "DELETE" and a["path"] == "/disciplines/SK" and a["body"] is None
    assert asking.delete(f"/api/projects/{slug}{a['path']}").status_code == 200
    assert set(names()) >= {"SP"} and "SK" not in names()


def test_the_chat_is_told_the_engineer_is_the_office():
    assert "Never send them to an office manager" in ask_mod.ASK_SYSTEM
    assert "rename_discipline" in ask_mod.ACTIONS and "remove_discipline" in ask_mod.ACTIONS
