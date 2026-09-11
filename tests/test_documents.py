"""Documents: the site → building → discipline tree and the one agent call that lists what the folder is missing.

The model is faked: the fake agent calls the real tools with canned arguments, so what these tests exercise is the tool
validation (discipline codes, building names, checklist rows, exact file names, judgement words) and the route bookkeeping.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image

from closeout import api, documents, pipeline
from closeout.config import Settings
from closeout.store import Store


class FakeDocsAgent:
    calls: list[dict] = []          # what the fake will record, in order
    prompts: list[list[dict]] = []  # what the agent was given

    def __init__(self, model=None, tools=None, system_prompt="", callback_handler=None):
        self.tools = {getattr(t, "tool_name", getattr(t, "__name__", "tool")): t for t in tools}
        self.system_prompt = system_prompt

    def __call__(self, content):
        FakeDocsAgent.prompts.append(content)
        for c in FakeDocsAgent.calls:
            name = next(k for k in self.tools if c["tool"] in k)
            self.tools[name](**{k: v for k, v in c.items() if k != "tool"})

        class R:
            class metrics:
                accumulated_usage = {"inputTokens": 1200, "outputTokens": 150}
        return R()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(documents, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(documents, "Agent", FakeDocsAgent)
    FakeDocsAgent.calls, FakeDocsAgent.prompts = [], []
    settings = Settings(data_dir=tmp_path / "data", model_id="fake-model")
    c = TestClient(api.create_app(settings))
    c.settings = settings
    return c


def _seed(client, tmp_path) -> str:
    """Three buildings from the units, a fourth named only on a drawing, site plans for AR and EL, a letter in the folder."""
    slug = client.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    st = Store(client.settings.data_dir / "closeout.db")
    prj = st.project_by_slug(slug)
    st.set_project_model(prj["id"], {**prj["model"], "address": "6891 Elm St", "city": "Vancouver",
                                     "units": [{"label": "Unit A", "address": "#1 6895 Elm St", "levels": ["Main Floor", "Upper Floor"]},
                                               {"label": "Unit B", "address": "#1 6893 Elm St", "levels": ["Main Floor", "Upper Floor"]},
                                               {"label": "Unit E", "address": "#1 6897 Elm St", "levels": ["Main Floor"]}],
                                     "disciplines": [{"code": "AR", "name": "Architectural", "dated": "2025-09-24", "sheets": 4},
                                                     {"code": "EL", "name": "Electrical", "dated": "2026-04-21", "sheets": 1}]})
    img = tmp_path / "sheet.png"
    Image.new("RGB", (1200, 800), (250, 250, 245)).save(img, "PNG")
    docs = st.replace_documents(prj["id"], [
        {"rel_path": "AR/AR.pdf", "discipline": "AR", "dated": "2025-09-24", "pages": 4, "kind": "drawing", "is_current": 1, "sha256": "a", "size": 1},
        {"rel_path": "EL/EL.pdf", "discipline": "EL", "dated": "2026-04-21", "pages": 1, "kind": "drawing", "is_current": 1, "sha256": "b", "size": 1},
        {"rel_path": "PM/Fire Safety Plan rev2.pdf", "discipline": "PM", "dated": None, "pages": 3, "kind": "document", "is_current": 0, "sha256": "c", "size": 1}])
    sheets = st.replace_sheets(prj["id"], [
        {"document_id": docs[0], "page": 1, "discipline": "AR", "sheet_number": "1", "title": "SITE PLAN & NOTES", "image_path": str(img)},
        {"document_id": docs[0], "page": 2, "discipline": "AR", "sheet_number": "4", "title": "FLOOR PLAN 6895 Elm St", "image_path": str(img)},
        {"document_id": docs[0], "page": 3, "discipline": "AR", "sheet_number": "13", "title": "FLOOR PLAN & SECTION 6891 Elm St", "image_path": str(img)},
        {"document_id": docs[0], "page": 4, "discipline": "AR", "sheet_number": "A1", "title": "DETAILS", "image_path": str(img)},
        {"document_id": docs[1], "page": 1, "discipline": "EL", "sheet_number": "EL-01", "title": "SITE PLAN", "image_path": str(img)}])
    st.sheet_read(sheets[0], {"sheet_kind": "site_plan", "summary": "Whole site", "units": ["6895 Elm St", "6893 Elm St", "6897 Elm St", "6891 Elm St"]})
    st.sheet_read(sheets[1], {"sheet_kind": "floor_plan", "summary": "Two units", "units": ["6895 Elm St"], "levels": ["Main Floor", "Upper Floor"]})
    st.sheet_read(sheets[2], {"sheet_kind": "floor_plan", "summary": "One unit", "units": ["6891 Elm St"], "levels": ["Main Floor", "Upper Floor"]})
    st.sheet_read(sheets[3], {"sheet_kind": "details", "summary": "Typical details", "units": ["6891 Elm St"]})
    st.sheet_read(sheets[4], {"sheet_kind": "site_plan", "summary": "Service entry", "units": ["6891 Elm St"]})
    return slug


def test_site_tree_puts_site_sheets_on_the_site_and_finds_the_building_only_the_drawings_name(client, tmp_path):
    slug = _seed(client, tmp_path)
    tree = client.get(f"/api/projects/{slug}/documents/tree").json()
    names = [b["name"] for b in tree["buildings"]]
    assert names == ["6895 Elm St", "6893 Elm St", "6897 Elm St", "6891 Elm St"]
    fourth = tree["buildings"][3]
    assert fourth["from_drawings"] and fourth["units"] == [] and fourth["levels"] == ["Main Floor", "Upper Floor"]
    assert [e["ref"] for e in tree["buildings"][0]["disciplines"]["AR"]] == ["4"]
    assert [e["ref"] for e in fourth["disciplines"]["AR"]] == ["13"]
    # the details sheet names 6891 but is not a building kind, and the site plans name every building: all stay on the site
    assert sorted(e["ref"] for e in tree["site"]["disciplines"]["AR"]) == ["1", "A1"]
    assert [e["ref"] for e in tree["site"]["site_plans"]] == ["1", "EL-01"]
    assert tree["buildings"][1]["disciplines"] == {}


def test_review_keeps_only_claims_that_match_the_folder(client, tmp_path):
    slug = _seed(client, tmp_path)
    FakeDocsAgent.calls = [
        {"tool": "record_missing", "what": "Plumbing set", "why": "This folder has no plumbing set at all.", "discipline": "PL"},   # unknown code
        {"tool": "record_missing", "what": "Electrical floor plans", "why": "EL-01 is the only electrical sheet.", "discipline": "EL", "building": "6899 Elm St"},
        {"tool": "record_missing", "what": "Electrical floor plans", "why": "EL-01 is the only electrical sheet; work is acceptable.", "discipline": "EL"},
        {"tool": "record_missing", "what": "Electrical floor plans", "why": "EL-01 is the only electrical sheet read.", "discipline": "EL", "building": "6893 Elm St"},
        {"tool": "record_missing", "what": "Electrical floor plans", "why": "Same thing said twice.", "discipline": "EL"},                  # duplicate
        {"tool": "record_missing", "what": "Architectural site plan", "why": "Already listed by the rules.", "discipline": "AR"},          # in `already`
        {"tool": "record_missing", "what": "Fire alarm verification certificate and report", "why": "No file name mentions fire alarm verification.",
         "checklist": "fire alarm verification certificate and report"},
        {"tool": "record_on_file", "checklist": "Fire safety plan", "file": "Fire Safety Plan.pdf"},                                        # not the exact name
        {"tool": "record_on_file", "checklist": "Fire safety plan", "file": "Fire Safety Plan rev2.pdf"},
        {"tool": "record_question", "question": "Is a sprinkler system part of this contract, so that the fire protection schedules apply?", "discipline": "EL"},
        {"tool": "record_question", "question": "The city wants a survey.", "building": "6893 Elm St"},                                  # not a question
        {"tool": "record_question", "question": "Does the geotechnical letter cover the retaining wall at the lane?", "building": "6899 Elm St"},   # unknown building
        {"tool": "record_file", "file": "Fire Safety Plan rev2.pdf", "building": "6893 Elm St"},
        {"tool": "record_file", "file": "Fire Safety Plan.pdf", "building": "6893 Elm St"},                                              # not the exact name
        {"tool": "record_summary", "summary": "Two drawing sets are on file. The electrical set is a site plan only and no letters of assurance are in the folder yet."},
    ]
    r = client.post(f"/api/projects/{slug}/documents/review", json={"already": ["Architectural site plan"]})
    assert r.status_code == 200, r.text
    out = r.json()["docs_review"]
    assert [m["what"] for m in out["missing"]] == ["Electrical floor plans", "Fire alarm verification certificate and report"]
    assert out["missing"][0]["building"] == "6893 Elm St" and out["missing"][0]["discipline"] == "EL"
    assert out["missing"][1]["checklist"] == "Fire alarm verification certificate and report"
    assert out["on_file"] == [{"checklist": "Fire safety plan", "file": "Fire Safety Plan rev2.pdf"}]
    assert out["summary"].startswith("Two drawing sets")
    assert out["questions"] == [{"question": "Is a sprinkler system part of this contract, so that the fire protection schedules apply?",
                                 "discipline": "EL", "building": "", "answer": ""}]
    assert out["placed"] == [{"file": "Fire Safety Plan rev2.pdf", "building": "6893 Elm St", "discipline": ""}]
    assert len(out["rejections"]) == 9 and out["usage"]["inputTokens"] == 1200
    assert out["buildings"] == ["6895 Elm St", "6893 Elm St", "6897 Elm St", "6891 Elm St"]
    # the agent got text only: the buildings, the sheets, the file names, the checklist and the rule-listed gaps
    text = FakeDocsAgent.prompts[0][0]["text"]
    for needle in ("BUILDINGS ON THE SITE (4)", "6891 Elm St · units: none", "EL-01 · SITE PLAN [site_plan]", "Fire Safety Plan rev2.pdf",
                   "Schedule C-B, electrical", "- Architectural site plan"):
        assert needle in text, needle
    assert all("image" not in part for part in FakeDocsAgent.prompts[0])
    # stored on the project and shown on the project page, with its run
    detail = client.get(f"/api/projects/{slug}").json()
    assert detail["docs_review"]["summary"] == out["summary"] and detail["docs_review"]["run_id"] == out["run_id"]
    assert len(detail["occupancy_docs"]) == 41 and detail["occupancy_docs"][0][1] == "Occupancy permit application"
    run = next(x for x in detail["runs"] if x["id"] == out["run_id"])
    assert run["kind"] == "documents" and run["status"] == "done"
    # the engineer fills in the blank the agent left; no agent call, stored with the review
    r = client.post(f"/api/projects/{slug}/documents/answer", json={"index": 0, "answer": "Yes,  NFPA 13D  in every unit."})
    assert r.status_code == 200 and r.json()["docs_review"]["questions"][0]["answer"] == "Yes, NFPA 13D in every unit."
    assert client.get(f"/api/projects/{slug}").json()["docs_review"]["questions"][0]["answer"] == "Yes, NFPA 13D in every unit."
    assert client.post(f"/api/projects/{slug}/documents/answer", json={"index": 4, "answer": "x"}).status_code == 404


def test_review_needs_drawings_and_fails_cleanly_when_the_agent_records_nothing(client, tmp_path):
    slug = client.post("/api/projects/blank", json={"name": "Empty"}).json()["slug"]
    assert client.post(f"/api/projects/{slug}/documents/review").status_code == 400
    slug = _seed(client, tmp_path)
    FakeDocsAgent.calls = [{"tool": "record_summary", "summary": "short"}]
    r = client.post(f"/api/projects/{slug}/documents/review")
    assert r.status_code == 502
    detail = client.get(f"/api/projects/{slug}").json()
    assert detail["docs_review"] is None
    assert detail["runs"][-1]["status"] == "failed" and detail["runs"][-1]["kind"] == "documents"


def test_scope_toggle_round_trips(client, tmp_path):
    """A row ticked out of scope drops into docs_scope and comes back out; unknown names are refused."""
    slug = _seed(client, tmp_path)
    r = client.post(f"/api/projects/{slug}/documents/scope", json={"name": "Envelope", "in_scope": False})
    assert r.status_code == 200 and r.json()["docs_scope"] == ["Envelope"]
    assert client.get(f"/api/projects/{slug}").json()["docs_scope"] == ["Envelope"]
    r = client.post(f"/api/projects/{slug}/documents/scope", json={"name": "Envelope", "in_scope": True})
    assert r.json()["docs_scope"] == []
    assert client.post(f"/api/projects/{slug}/documents/scope", json={"name": "Moon survey", "in_scope": False}).status_code == 404


def test_a_document_row_opens_the_file_from_the_project_folder(client, tmp_path):
    slug = _seed(client, tmp_path)
    st = Store(client.settings.data_dir / "closeout.db")
    prj = st.project_by_slug(slug)
    root = tmp_path / "folder"
    st.upsert_project(slug, prj["name"], str(root))
    (root / "PM").mkdir(parents=True, exist_ok=True)
    (root / "PM" / "Fire Safety Plan rev2.pdf").write_bytes(b"%PDF-1.4 fake")
    doc = next(d for d in st.documents(prj["id"]) if d["kind"] == "document")
    r = client.get(f"/api/projects/{slug}/documents/{doc['id']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "inline" in r.headers["content-disposition"]
    assert client.get(f"/api/projects/{slug}/documents/doc_nope/file").status_code == 404
    (root / "PM" / "Fire Safety Plan rev2.pdf").unlink()
    assert client.get(f"/api/projects/{slug}/documents/{doc['id']}/file").status_code == 404


# --- filing: Closeout may place a file, the engineer can always move or rename it, and every change can be undone ----------

def test_closeout_placements_become_filings_the_engineer_can_override_and_undo(client, tmp_path):
    slug = _seed(client, tmp_path)
    FakeDocsAgent.calls = [
        {"tool": "record_file", "file": "Fire Safety Plan rev2.pdf", "building": "6893 Elm St"},
        {"tool": "record_summary", "summary": "One letter is filed under a building; the rest of the folder stays on the site."},
    ]
    j = client.post(f"/api/projects/{slug}/documents/review", json={}).json()
    assert j["filings"]["Fire Safety Plan rev2.pdf"]["who"] == "closeout"
    assert j["filings"]["Fire Safety Plan rev2.pdf"]["building"] == "6893 Elm St"
    # a wrong file, building or discipline is refused with a plain reason
    assert client.post(f"/api/projects/{slug}/filing", json={"file": "nope.pdf", "building": "6895 Elm St"}).status_code == 400
    assert "unknown building" in client.post(f"/api/projects/{slug}/filing", json={"file": "Fire Safety Plan rev2.pdf", "building": "6899 Elm St"}).json()["detail"]
    assert "unknown discipline" in client.post(f"/api/projects/{slug}/filing", json={"file": "Fire Safety Plan rev2.pdf", "discipline": "PL"}).json()["detail"]
    assert "already" in client.post(f"/api/projects/{slug}/filing", json={"file": "Fire Safety Plan rev2.pdf", "building": "6893 Elm St"}).json()["detail"]
    # the engineer moves it to another building and gives it a name; only what they name changes
    j = client.post(f"/api/projects/{slug}/filing", json={"file": "Fire Safety Plan rev2.pdf", "building": "6895 Elm St", "name": "Fire safety plan (revision 2)"}).json()
    cur = j["filings"]["Fire Safety Plan rev2.pdf"]
    assert (cur["who"], cur["building"], cur["discipline"], cur["name"]) == ("engineer", "6895 Elm St", "", "Fire safety plan (revision 2)")
    j = client.post(f"/api/projects/{slug}/filing", json={"file": "Fire Safety Plan rev2.pdf", "discipline": "el"}).json()
    cur = j["filings"]["Fire Safety Plan rev2.pdf"]
    assert (cur["building"], cur["discipline"], cur["name"]) == ("6895 Elm St", "EL", "Fire safety plan (revision 2)")
    assert [h["who"] for h in j["history"]] == ["closeout", "engineer", "engineer"]
    # a later review by Closeout never overrides the engineer
    FakeDocsAgent.calls = [{"tool": "record_file", "file": "Fire Safety Plan rev2.pdf", "building": "6897 Elm St"}, {"tool": "record_summary", "summary": "The same folder looked at a second time, nothing else is new."}]
    j = client.post(f"/api/projects/{slug}/documents/review", json={}).json()
    assert j["filings"]["Fire Safety Plan rev2.pdf"]["building"] == "6895 Elm St" and len(client.get(f"/api/projects/{slug}/filing").json()["history"]) == 3
    # undo walks back one step at a time, down to Closeout's own placement, then to nothing
    j = client.post(f"/api/projects/{slug}/filing/undo", json={"file": "Fire Safety Plan rev2.pdf"}).json()
    assert j["filings"]["Fire Safety Plan rev2.pdf"]["discipline"] == "" and j["filings"]["Fire Safety Plan rev2.pdf"]["building"] == "6895 Elm St"
    client.post(f"/api/projects/{slug}/filing/undo", json={"file": "Fire Safety Plan rev2.pdf"})
    j = client.post(f"/api/projects/{slug}/filing/undo", json={"file": "Fire Safety Plan rev2.pdf"}).json()
    assert j["filings"] == {} and j["history"] == []
    assert client.post(f"/api/projects/{slug}/filing/undo", json={"file": "Fire Safety Plan rev2.pdf"}).status_code == 404
    detail = client.get(f"/api/projects/{slug}").json()
    assert detail["filings"] == {} and detail["filing_history"] == []
