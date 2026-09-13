"""Project intake on a tiny generated drawing set, with the sheet/summary agents faked (no Bedrock)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from closeout import project
from closeout.config import Settings
from closeout.store import Store

ARCH = (36 * 72, 24 * 72)


def _pdf(path: Path, pages: list[list[str]], size=ARCH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=size)
    for lines in pages:
        y = size[1] - 60
        for ln in lines:
            c.drawString(40, y, ln)
            y -= 16
        c.showPage()
    c.save()


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "24-0001_Sample"
    _pdf(root / "AR/250101_old/set.pdf", [["OLD SET"]])
    _pdf(root / "AR/250301_latest/set.pdf", [
        ["DRAWING INDEX", "1        SITE PLAN & NOTES", "2        FLOOR PLAN 12 Sample St", "SHEET NUMBER", "1"],
        ["FLOOR PLAN", "KITCHEN", "LIVING", "SHEET NUMBER", "2"],
    ])
    _pdf(root / "EL/250401_issued/24-0001_EL_250401.pdf", [["SITE PLAN & EL CLOSET", "DRAWING #:", "EL-01"]])
    _pdf(root / "PM/DD/250401_EL_sent/Sent/24-0001_EL_250401.pdf", [["SITE PLAN & EL CLOSET", "DRAWING #:", "EL-01"]])
    _pdf(root / "BCH/250201_client/Design Intake Letter.pdf", [["Dear client"]], size=letter)
    return root


def test_scan_classifies_dates_and_current_sets(folder):
    docs = project.scan_folder(folder)
    by = {d.rel_path: d for d in docs}
    assert by["BCH/250201_client/Design Intake Letter.pdf"].kind == "document"
    assert by["AR/250301_latest/set.pdf"].is_current and not by["AR/250101_old/set.pdf"].is_current
    assert by["EL/250401_issued/24-0001_EL_250401.pdf"].is_current            # discipline folder beats the PM copy
    assert not by["PM/DD/250401_EL_sent/Sent/24-0001_EL_250401.pdf"].is_current
    assert by["PM/DD/250401_EL_sent/Sent/24-0001_EL_250401.pdf"].discipline == "EL"
    assert by["AR/250301_latest/set.pdf"].dated == "2025-03-01"
    assert by["EL/250401_issued/24-0001_EL_250401.pdf"].dated == "2025-04-01"


def test_import_renders_indexes_and_reads(folder, tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", model_id="fake")
    store = Store(settings.data_dir / "closeout.db")

    def fake_sheet(store_, run_id, job_id, sheet_id, index, model=None, settings=None):
        sh = store_.sheet(sheet_id)
        store_.sheet_read(sheet_id, {"sheet_kind": "floor_plan", "summary": "fake", "levels": ["Main floor"], "units": ["Unit A"],
                                     "spaces": [{"name": "Kitchen", "unit": "Unit A", "level": "Main floor"}], "elements": []},
                          sheet_number=sh["sheet_number"] or "X", title=sh["title"] or "Fake title")
        return {"usage": {"inputTokens": 1}}

    def fake_summary(store_, run_id, job_id, project_id, model=None, settings=None):
        m = store_.project(project_id)["model"]
        m.update({"name": "12 Sample St", "address": "12 Sample St", "city": "Sampleville", "units": [{"label": "Unit A", "address": "12 Sample St", "levels": ["Main floor"]}],
                  "levels": [{"name": "Main floor", "elevation": "100.00'"}]})
        store_.set_project_model(project_id, m)
        store_.conn.execute("UPDATE projects SET name=? WHERE id=?", ("12 Sample St", project_id)); store_.conn.commit()
        return {"usage": {"inputTokens": 1}}

    monkeypatch.setattr(project, "run_sheet_job", fake_sheet)
    monkeypatch.setattr(project, "run_project_summary_job", fake_summary)
    monkeypatch.setattr(project, "make_model", lambda s: None)
    events = []
    res = project.import_project(store, folder, "sample", settings, progress=lambda e, d: events.append(e))
    assert res["status"] == "done"
    view = project.project_view(store)
    assert view["name"] == "12 Sample St"
    assert view["drawing_index"] == {"1": "SITE PLAN & NOTES", "2": "FLOOR PLAN 12 Sample St"}
    nums = sorted((s["discipline"], s["sheet_number"]) for s in view["sheets"])
    assert nums == [("AR", "1"), ("AR", "2"), ("EL", "EL-01")]
    assert all(Path(s["image_path"]).exists() for s in view["sheets"])
    assert view["spaces"][0]["name"] == "Kitchen" and view["spaces"][0]["sheets"]
    assert events[-1] == "project_ready" and "sheet_rendered" in events
    assert json.loads((settings.data_dir / "projects/sample/project.json").read_text())["name"] == "12 Sample St"
    assert "Kitchen" in project.sheet_text_for_agent(store)


def test_every_file_keeps_a_log_across_drops(folder, tmp_path, monkeypatch):
    """The folder is dropped twice. The log per file name says what came in, what changed, which set is the one to walk
    with now, and what left the folder; the project payload carries it for the file's own folder on the Documents tab."""
    from fastapi.testclient import TestClient
    from closeout import api
    settings = Settings(data_dir=tmp_path / "data", model_id="fake")
    store = Store(settings.data_dir / "closeout.db")
    monkeypatch.setattr(project, "make_model", lambda s: None)
    project.import_project(store, folder, "sample", settings, read_with_model=False)
    log = store.document_log(store.project_by_slug("sample")["id"])
    kinds = {(e["file"], e["kind"]) for e in log}
    assert ("set.pdf", "received") in kinds and ("set.pdf", "current") in kinds
    assert ("Design Intake Letter.pdf", "received") in kinds and ("Design Intake Letter.pdf", "current") not in kinds
    assert all(e["kind"] in ("received", "current") for e in log)
    # a newer AR issue arrives, the letter is re-saved with new contents, the old AR issue is cleaned out of the folder
    _pdf(folder / "AR/250601_newest/set.pdf", [["NEWEST SET", "SHEET NUMBER", "1"]])
    _pdf(folder / "BCH/250201_client/Design Intake Letter.pdf", [["Dear client", "revised"]], size=letter)
    for f in (folder / "AR/250101_old").iterdir():
        f.unlink()
    (folder / "AR/250101_old").rmdir()
    project.import_project(store, folder, "sample", settings, read_with_model=False)
    pid = store.project_by_slug("sample")["id"]
    later = store.document_log(pid)[len(log):]
    assert {(e["file"], e["kind"]) for e in later} == {("set.pdf", "updated"), ("Design Intake Letter.pdf", "updated")}
    assert [e["kind"] for e in store.document_log(pid, "set.pdf")] == ["received", "current", "updated"]
    assert next(e for e in later if e["file"] == "set.pdf")["rel_path"] == "AR/250601_newest/set.pdf"
    # the untouched EL set writes nothing the second time
    assert not [e for e in later if "EL" in e["file"]]
    client = TestClient(api.create_app(settings))
    detail = client.get("/api/projects/sample").json()
    assert len(detail["document_log"]) == len(log) + 2
    assert detail["document_log"][0]["kind"] == "received"


def test_a_set_date_is_read_in_the_office_style_and_the_iso_style():
    from closeout.project import _dated
    assert _dated("EL/260421_ADDED HOUSE PANEL/E1.pdf") == "2026-04-21"
    assert _dated("AR/Cedar Row - Architectural Set 2026-08-28.pdf") == "2026-08-28"
    assert _dated("AR/260101/Set 2026-08-28.pdf") == "2026-08-28"      # the file's own date wins over the folder's
    assert _dated("PM/notes.pdf") is None
