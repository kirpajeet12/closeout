"""The field review report: deterministic, printable, built from the saved walk. Fictional project, no model."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from closeout import api, pipeline, plans, review
from closeout.config import Settings
from closeout.store import Store


def _png(path, size=(1200, 800)):
    Image.new("RGB", size, (250, 250, 250)).save(path)


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (120, 120, 120)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    for mod in (pipeline, review, plans):
        monkeypatch.setattr(mod, "make_model", lambda settings, fast=False: None)
    settings = Settings(data_dir=tmp_path / "data", model_id="fake-model", office="Elm Street Engineering")
    c = TestClient(api.create_app(settings))
    c.settings = settings
    return c


def _seed(client, tmp_path):
    slug = client.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    st = Store(client.settings.data_dir / "closeout.db")
    prj = st.project_by_slug(slug)
    st.set_project_model(prj["id"], {**prj["model"], "units": [{"label": "Unit C", "levels": ["Main Floor", "Upper Floor"]}],
                                     "levels": [{"name": "Main Floor"}, {"name": "Upper Floor"}]})
    img = tmp_path / "EL-2.png"
    _png(img)
    old, cur = st.replace_documents(prj["id"], [
        {"rel_path": "Electrical/EL-2026-01-01.pdf", "discipline": "EL", "dated": "2026-01-01", "pages": 4, "kind": "drawing", "is_current": 0, "sha256": "a", "size": 1},
        {"rel_path": "Electrical/EL-2026-03-01.pdf", "discipline": "EL", "dated": "2026-03-01", "pages": 5, "kind": "drawing", "is_current": 1, "sha256": "b", "size": 1}])
    sid = st.replace_sheets(prj["id"], [{"document_id": cur, "page": 2, "discipline": "EL", "sheet_number": "EL-2",
                                         "title": "UPPER FLOOR POWER PLAN", "image_path": str(img)}])[0]
    return slug, sid


def test_report_lists_every_item_with_status_pictures_and_drawings(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": 0.3, "pin_y": 0.4, "review_id": rev["id"],
                "location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": "Receptacle beside the basin has no cover plate.",
                "evidence_required": "photo: completed", "unit": "Unit C", "level": "Upper Floor", "space": "Bath 2"},
                files={"photo": ("IMG_0002.jpg", _jpeg_bytes(), "image/jpeg")})
    client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": 0.6, "pin_y": 0.4, "review_id": rev["id"],
                "location": "Unit C, Upper Floor, hall", "description": "Panel schedule card is missing from the sub-panel door.",
                "evidence_required": "photo: completed", "unit": "Unit C", "level": "Upper Floor"})
    # a report exists while the walk is still open, marked as such
    page = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/report")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "draft" in page.text and "In progress" in page.text and "EL-01" in page.text and "EL-02" in page.text

    client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish")
    client.post(f"/api/projects/{slug}/items/EL-01/decision", json={"decision": "accept", "note": "Cover plate seen on the 12th"})
    data = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/report.json").json()
    assert data["count"] == 2 and data["photos"] == 1 and data["units"] == ["Unit C"] and data["sheets_used"] == ["EL-2"]
    assert data["office"] == "Elm Street Engineering" and data["project"]["name"] == "Row Houses"
    assert data["name"].startswith("FieldReview_Row-Houses_") and data["name"].endswith("_EL1")
    one, two = data["items"]
    assert one["status"] == "Ready to close" and one["status_note"] == "Cover plate seen on the 12th" and two["status"] == "Open"
    assert one["photo_url"].endswith("/register/EL-01/reference") and one["plan_url"].endswith("/items/EL-01/pin.jpg")
    assert two["photo_url"] == "" and two["plan_url"].endswith("/items/EL-02/pin.jpg")
    assert one["sheet_title"] == "UPPER FLOOR POWER PLAN" and data["status_counts"] == {"Ready to close": 1, "Open": 1}
    # the drawings on file for that discipline, current issue first
    assert [(d["file"], d["current"]) for d in data["drawings"]] == [("EL-2026-03-01.pdf", True), ("EL-2026-01-01.pdf", False)]
    assert data["current_set"]["dated"] == "2026-03-01"
    # the printable page carries all of it, without a draft mark, and its pictures resolve
    page = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/report").text
    assert "Field review report" in page and 'class="tag"' not in page and "Elm Street Engineering" in page
    assert "Receptacle beside the basin" in page and "Ready to close" in page and "EL-2026-03-01.pdf" in page and "Superseded" in page
    assert "Reviewed by" in page and page.count("Prepared by Elm Street Engineering") == 1
    pin = client.get(f"/api/projects/{slug}/items/EL-01/pin.jpg")
    assert pin.status_code == 200 and pin.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(pin.content)).size[0] > 100
    assert client.get(f"/api/projects/{slug}/register/EL-01/reference").status_code == 200
    # unknown review
    assert client.get(f"/api/projects/{slug}/reviews/rev_nothing/report").status_code == 404
