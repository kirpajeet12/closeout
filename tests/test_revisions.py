"""Revision compare: two issues of the same set, read from the words printed on the sheets. No model call anywhere."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from closeout import api, revisions
from closeout.config import Settings
from closeout.store import Store

ARCH = (36 * 72, 24 * 72)


def _pdf(path: Path, pages: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=ARCH)
    for lines in pages:
        y = ARCH[1] - 60
        for ln in lines:
            c.drawString(40, y, ln)
            y -= 16
        c.showPage()
    c.save()


def _sheet(number: str, title: str, *body: str) -> list[str]:
    return [*body, "DRAWING TITLE:", title, "DRAWING #:", number]


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", model_id="fake-model")
    c = TestClient(api.create_app(settings))
    c.settings = settings
    return c


def _seed(client, tmp_path) -> tuple[str, dict[str, str]]:
    root = tmp_path / "24-0001_Row Houses"
    _pdf(root / "EL/241125_issued/EL_241125.pdf", [
        _sheet("E1", "SITE PLAN", "SERVICE 200A 240V", "PANEL A IN GARAGE"),
        _sheet("E2", "MAIN FLOOR PLAN", "KITCHEN RECEPTACLES 20A", "RANGE 40A"),
        _sheet("E3", "SINGLE LINE DIAGRAM", "MAIN BREAKER 200A", "ISSUED 25 NOV 2024"),
    ])
    _pdf(root / "EL/260421_house panel/EL_260421.pdf", [
        _sheet("E1", "SITE PLAN", "SERVICE 400A 240V", "PANEL A IN GARAGE", "HOUSE PANEL ADDED"),
        _sheet("E3", "SINGLE LINE DIAGRAM", "MAIN BREAKER 200A", "ISSUED 21 APR 2026"),
        _sheet("E4", "MAIN FLOOR PLAN", "KITCHEN RECEPTACLES 20A", "RANGE 40A"),
        _sheet("E5", "LOAD CALCULATIONS", "TOTAL LOAD 312A"),
    ])
    _pdf(root / "PL/250128_issued/PL_250128.pdf", [_sheet("PL-01", "SITE PLAN", "WATER SERVICE 25MM")])
    _pdf(root / "PM/Letter.pdf", [["Dear client"]])
    slug = client.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    st = Store(client.settings.data_dir / "closeout.db")
    prj = st.project_by_slug(slug)
    st.upsert_project(slug, "Row Houses", str(root))
    ids = st.replace_documents(prj["id"], [
        {"rel_path": "EL/241125_issued/EL_241125.pdf", "discipline": "EL", "dated": "2024-11-25", "pages": 3, "kind": "drawing", "is_current": 0, "sha256": "a", "size": 1},
        {"rel_path": "EL/260421_house panel/EL_260421.pdf", "discipline": "EL", "dated": "2026-04-21", "pages": 4, "kind": "drawing", "is_current": 1, "sha256": "b", "size": 1},
        {"rel_path": "PL/250128_issued/PL_250128.pdf", "discipline": "PL", "dated": "2025-01-28", "pages": 1, "kind": "drawing", "is_current": 1, "sha256": "c", "size": 1},
        {"rel_path": "PM/Letter.pdf", "discipline": "PM", "dated": None, "pages": 1, "kind": "document", "is_current": 0, "sha256": "d", "size": 1}])
    return slug, {"old": ids[0], "new": ids[1], "pl": ids[2], "letter": ids[3]}


def test_sheets_are_read_from_the_title_block(tmp_path):
    pdf = tmp_path / "set.pdf"
    _pdf(pdf, [_sheet("E1", "SITE PLAN", "SERVICE 200A"), ["COVER", "DRAWING TITLE", "2", "SHEET NUMBER"]])
    sheets = revisions.read_issue(pdf, 2)
    assert (sheets[0]["number"], sheets[0]["title"]) == ("E1", "SITE PLAN")
    assert "SERVICE 200A" in sheets[0]["lines"]
    assert sheets[1]["number"] == "2" and sheets[1]["title"] == "COVER"


def test_compare_lists_added_removed_renumbered_and_changed_sheets(client, tmp_path):
    slug, ids = _seed(client, tmp_path)
    r = client.post(f"/api/projects/{slug}/revisions/compare", json={"old_document_id": ids["old"], "new_document_id": ids["new"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [s["number"] for s in out["added"]] == ["E5"]
    assert out["removed"] == []
    assert [(s["was"], s["number"]) for s in out["renumbered"]] == [("E2", "E4")]
    changed = {s["number"]: s for s in out["changed"]}
    assert set(changed) == {"E1"}, "a re-dated sheet with the same words is not a change"
    assert changed["E1"]["now_says"] == ["HOUSE PANEL ADDED", "SERVICE 400A 240V"]
    assert changed["E1"]["no_longer_says"] == ["SERVICE 200A 240V"]
    assert [s["number"] for s in out["unchanged"]] == ["E3"]
    assert out["old"]["sheets"] == 3 and out["new"]["sheets"] == 4 and out["new"]["file"] == "EL_260421.pdf"
    assert out["basis"].startswith("Compared from the words printed")
    # the plumbing set is dated before this issue and has not been revised since: the office may need to tell them
    assert [(w["discipline"], w["dated"]) for w in out["who_else"]] == [("PL", "2025-01-28")]


def test_compare_refuses_the_wrong_pairs(client, tmp_path):
    slug, ids = _seed(client, tmp_path)
    post = lambda a, b: client.post(f"/api/projects/{slug}/revisions/compare", json={"old_document_id": a, "new_document_id": b})
    assert post(ids["old"], "doc_nope").status_code == 404
    assert post(ids["old"], ids["pl"]).status_code == 400
    assert post(ids["letter"], ids["new"]).status_code == 400


def test_issue_list_is_per_discipline_oldest_first(client, tmp_path):
    slug, ids = _seed(client, tmp_path)
    rows = client.get(f"/api/projects/{slug}/revisions").json()["issues"]
    assert [(r["discipline"], r["dated"], r["is_current"]) for r in rows] == [("EL", "2024-11-25", False), ("EL", "2026-04-21", True), ("PL", "2025-01-28", True)]
    assert rows[0]["file"] == "EL_241125.pdf" and rows[0]["name"] == "Electrical"
