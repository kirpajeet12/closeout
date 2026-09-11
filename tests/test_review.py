"""Field review: start a numbered review per discipline, pin a spot on a sheet, get the agent's proposal, save it.

The model is faked: the fake "agent" calls the real record_field_note tool, so the tool's validation
(evidence slots, forbidden words, unit/level names) is what these tests exercise.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from closeout import api, pipeline, plans, review
from closeout.config import Settings
from closeout.store import Store


def _png(path, size=(1200, 800)):
    im = Image.new("RGB", size, (250, 250, 245))
    im.save(path, "PNG")


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (120, 120, 120)).save(buf, "JPEG")
    return buf.getvalue()


class FakeFieldAgent:
    """Stands in for strands.Agent: takes the tools and calls record_field_note with canned arguments."""
    proposals: list[dict] = []
    messages: list[dict] = []
    calls: list[list[dict]] = []

    def __init__(self, model=None, tools=None, system_prompt="", callback_handler=None):
        self.tools = {t.tool_name if hasattr(t, "tool_name") else getattr(t, "__name__", "tool"): t for t in tools}
        self.system_prompt = system_prompt

    def __call__(self, content):
        FakeFieldAgent.calls.append(content)
        if any("record_message" in name for name in self.tools):
            record = next(t for name, t in self.tools.items() if "record_message" in name)
            proposals = FakeFieldAgent.messages
        elif any("record_plan" in name for name in self.tools):
            record = next(t for name, t in self.tools.items() if "record_plan" in name)
            for args in FakeFieldAgent.proposals:   # one call per plan drawing, rejected ones included
                record(**args)
            proposals = []
        else:
            record = next(t for name, t in self.tools.items() if "record_field_note" in name)
            proposals = FakeFieldAgent.proposals
        for args in proposals:
            out = record(**args)
            if out == "recorded":
                break

        class R:
            class metrics:
                accumulated_usage = {"inputTokens": 900, "outputTokens": 80}
        return R()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(review, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(review, "Agent", FakeFieldAgent)
    monkeypatch.setattr(plans, "make_model", lambda settings, fast=False: None)
    monkeypatch.setattr(plans, "Agent", FakeFieldAgent)
    FakeFieldAgent.proposals = []
    FakeFieldAgent.messages = []
    FakeFieldAgent.calls = []
    settings = Settings(data_dir=tmp_path / "data", model_id="fake-model")
    c = TestClient(api.create_app(settings))
    c.settings = settings
    return c


def _seed(client, tmp_path) -> tuple[str, str]:
    """A project with one electrical sheet whose reading names Unit C / Upper Floor / Bath 2."""
    r = client.post("/api/projects/blank", json={"name": "Row Houses"})
    slug = r.json()["slug"]
    st = Store(client.settings.data_dir / "closeout.db")
    prj = st.project_by_slug(slug)
    st.set_project_model(prj["id"], {**prj["model"], "units": [{"label": "Unit C", "levels": ["Main Floor", "Upper Floor"]}],
                                     "levels": [{"name": "Main Floor"}, {"name": "Upper Floor"}]})
    img = tmp_path / "EL-2.png"
    _png(img)
    doc_id = st.replace_documents(prj["id"], [{"rel_path": "Electrical/EL.pdf", "discipline": "EL", "dated": "2026-01-01",
                                               "pages": 1, "kind": "drawing", "is_current": 1, "sha256": "x", "size": 1}])[0]
    sid = st.replace_sheets(prj["id"], [{"document_id": doc_id, "page": 2, "discipline": "EL", "sheet_number": "EL-2",
                                         "title": "UPPER FLOOR POWER PLAN", "image_path": str(img)}])[0]
    st.sheet_read(sid, {"sheet_kind": "plan", "spaces": [{"unit": "Unit C", "level": "Upper Floor", "name": "Bath 2"}]})
    return slug, sid


def test_start_review_numbers_per_discipline_and_resumes_the_active_one(client, tmp_path):
    slug, _ = _seed(client, tmp_path)
    r1 = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()
    assert r1["review"]["sequence"] == 1 and r1["review"]["title"] == "Field review 1" and not r1["resumed"]
    again = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "el"}).json()
    assert again["resumed"] and again["review"]["id"] == r1["review"]["id"]
    ar = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "AR"}).json()["review"]
    assert ar["sequence"] == 1 and ar["discipline"] == "AR"
    client.post(f"/api/projects/{slug}/reviews/{r1['review']['id']}/finish")
    r2 = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    assert r2["sequence"] == 2 and not any(x["id"] == r2["id"] for x in [r1["review"]])
    assert client.post(f"/api/projects/{slug}/reviews", json={"discipline": "XX"}).status_code == 400
    detail = client.get(f"/api/projects/{slug}").json()
    assert [x["status"] for x in detail["reviews"]] == ["finished", "active", "active"]


def test_suggest_sends_photo_pin_crops_and_context_and_validates_the_tool_call(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    FakeFieldAgent.proposals = [
        # first attempt: judgement word + no photo slot + unknown unit -> rejected, agent retries
        {"location": "Unit Q, Upper Floor, Bath 2: wall behind toilet", "description": "Work is acceptable here.",
         "evidence_required": "report: test sheet", "discipline": "EL", "unit": "Unit Q", "level": "Upper Floor", "space": "Bath 2"},
        {"location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": "Receptacle beside the basin has no GFCI protection and the cover plate is missing.",
         "evidence_required": "photo: GFCI receptacle installed with cover plate; report: electrician's test sheet",
         "discipline": "EL", "unit": "Unit C", "level": "Upper Floor", "space": "Bath 2"},
    ]
    r = client.post(f"/api/projects/{slug}/findings/suggest",
                    data={"sheet_id": sid, "pin_x": 0.62, "pin_y": 0.41, "note": "no gfci", "discipline": "EL"},
                    files={"photo": ("IMG_0001.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    s = r.json()["suggestion"]
    assert s["location"].startswith("Unit C, Upper Floor, Bath 2") and s["unit"] == "Unit C"
    assert [x["type"] for x in s["slots"]] == ["photo", "report"]
    assert s["usage"]["inputTokens"] == 900
    assert len(s["rejections"]) == 1 and "forbidden" in s["rejections"][0]
    content = FakeFieldAgent.calls[-1]
    images = [c for c in content if "image" in c]
    assert len(images) == 3, "photo, pin close-up, whole sheet"
    texts = " ".join(c["text"] for c in content if "text" in c)
    assert "Bath 2" in texts and "ENGINEER'S NOTE: no gfci" in texts and "x=0.620" in texts
    # pin outside the sheet, or a sheet from another project, is refused before any model call
    assert client.post(f"/api/projects/{slug}/findings/suggest", data={"sheet_id": sid, "pin_x": 1.4, "pin_y": 0.2}).status_code == 400
    assert client.post(f"/api/projects/{slug}/findings/suggest", data={"sheet_id": "sht_nope", "pin_x": 0.4, "pin_y": 0.2}).status_code == 404


def test_suggest_reports_when_the_agent_records_nothing(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    FakeFieldAgent.proposals = [{"location": "x", "description": "y", "evidence_required": "", "discipline": "EL"}]
    r = client.post(f"/api/projects/{slug}/findings/suggest", data={"sheet_id": sid, "pin_x": 0.5, "pin_y": 0.5})
    assert r.status_code == 502 and "location too short" in r.text


def test_save_finding_numbers_pins_and_keeps_the_photo(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    body = {"sheet_id": sid, "pin_x": 0.62, "pin_y": 0.41, "review_id": rev["id"],
            "location": "Unit C, Upper Floor, Bath 2: wall behind toilet",
            "description": "Receptacle beside the basin has no GFCI protection.",
            "evidence_required": "photo: GFCI receptacle installed", "unit": "Unit C", "level": "Upper Floor", "space": "Bath 2", "note": "no gfci"}
    r = client.post(f"/api/projects/{slug}/findings", data=body, files={"photo": ("IMG_0001.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["item_id"] == "EL-01" and item["source"] == "field" and item["sheet"] == "EL-2" and item["sheet_id"] == sid
    assert item["pin_x"] == 0.62 and item["review_id"] == rev["id"] and item["slots"][0]["type"] == "photo"
    assert item["reference_photo"].endswith("/projects/row-houses/field/EL-01.jpg")
    assert item["ref_meta"]["original_name"] == "IMG_0001.jpg" and item["ref_meta"]["width"] == 640
    assert client.get(f"/api/projects/{slug}/register/EL-01/reference").status_code == 200

    second = client.post(f"/api/projects/{slug}/findings", data={**body, "location": "Unit C, Main Floor, Kitchen: island"}).json()["item"]
    assert second["item_id"] == "EL-02" and second["reference_photo"] == ""
    # the project page lists both, with pins
    reg = client.get(f"/api/projects/{slug}").json()["register"]
    assert [d["item_id"] for d in reg] == ["EL-01", "EL-02"]

    # edit, then delete; the number is never reused
    p = client.patch(f"/api/projects/{slug}/findings/EL-02", json={"description": "Island receptacle is loose in its box.",
                                                                    "evidence_required": "photo: done; letter: contractor's confirmation"})
    assert p.status_code == 200 and [s["type"] for s in p.json()["item"]["slots"]] == ["photo", "letter"]
    assert client.patch(f"/api/projects/{slug}/findings/EL-02", json={"evidence_required": "video: clip"}).status_code == 400
    assert client.delete(f"/api/projects/{slug}/findings/EL-02").json()["deleted"] == "EL-02"
    third = client.post(f"/api/projects/{slug}/findings", data=body).json()["item"]
    assert third["item_id"] == "EL-03"

    # bad slots and a finished review are refused
    assert client.post(f"/api/projects/{slug}/findings", data={**body, "evidence_required": "selfie: me"}).status_code == 400
    client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish")
    assert client.post(f"/api/projects/{slug}/findings", data=body).status_code == 409


def test_pin_images_mark_the_spot(tmp_path):
    img = tmp_path / "s.png"
    _png(img, (2000, 1400))
    crop, whole = review.pin_images(img, 0.25, 0.75)
    c = Image.open(io.BytesIO(crop)); w = Image.open(io.BytesIO(whole))
    assert max(w.size) <= 1568 and max(c.size) <= 1568
    # the pin colour is present in both renders
    assert any(abs(px[0] - 232) < 10 and abs(px[1] - 89) < 10 for px in c.getdata())
    assert any(abs(px[0] - 232) < 10 and abs(px[1] - 89) < 10 for px in w.getdata())


def test_locate_photo_first_picks_a_sheet_from_the_list(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    FakeFieldAgent.proposals = [
        {"location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": "Receptacle beside the basin has no cover plate.",
         "evidence_required": "photo: cover plate installed", "discipline": "EL", "unit": "Unit C", "level": "Upper Floor", "space": "Bath 2", "sheet": "EL-9"},
        {"location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": "Receptacle beside the basin has no cover plate.",
         "evidence_required": "photo: cover plate installed", "discipline": "EL", "unit": "Unit C", "level": "Upper Floor", "space": "Bath 2", "sheet": "EL-2"},
    ]
    r = client.post(f"/api/projects/{slug}/findings/locate", data={"discipline": "EL", "unit": "Unit C", "level": "Upper Floor", "gps": "49.2,-123.1 ±12m"},
                    files={"photo": ("IMG_0002.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    s = r.json()["suggestion"]
    assert s["sheet"] == "EL-2" and s["sheet_id"] == sid and "EL-2" in s["rejections"][0]
    texts = " ".join(c["text"] for c in FakeFieldAgent.calls[-1] if "text" in c)
    assert "EL-2: UPPER FLOOR POWER PLAN" in texts and "unit Unit C, level Upper Floor" in texts and "GPS" in texts
    assert client.post(f"/api/projects/{slug}/findings/locate", data={"discipline": "EL"}).status_code == 422
    assert client.post(f"/api/projects/{slug}/findings/locate", data={"discipline": "PL"}, files={"photo": ("a.jpg", _jpeg_bytes(), "image/jpeg")}).status_code == 400
    # gps rides along into the saved item's photo metadata
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    item = client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": 0.3, "pin_y": 0.3, "review_id": rev["id"],
                       "location": s["location"], "description": s["description"], "evidence_required": s["evidence_required"], "gps": "49.2,-123.1 ±12m"},
                       files={"photo": ("IMG_0002.jpg", _jpeg_bytes(), "image/jpeg")}).json()["item"]
    assert item["ref_meta"]["gps"] == "49.2,-123.1 ±12m"


def test_finish_review_builds_the_package_and_the_agent_drafts_the_covering_message(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    # an empty review finishes fine but has nothing to send
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish")
    assert r.status_code == 200 and r.json()["message"] is None and "nothing to send" in r.json()["error"]
    assert r.json()["package"]["count"] == 0 and r.json()["review"]["status"] == "finished"

    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    assert rev["sequence"] == 2
    for x, desc in ((0.3, "Receptacle beside the basin has no cover plate."), (0.6, "Panel schedule card is missing from the sub-panel door.")):
        client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": x, "pin_y": 0.4, "review_id": rev["id"],
                    "location": "Unit C, Upper Floor, Bath 2: wall behind toilet", "description": desc,
                    "evidence_required": "photo: completed", "unit": "Unit C", "level": "Upper Floor"},
                    files={"photo": ("IMG_0002.jpg", _jpeg_bytes(), "image/jpeg")})
    body_ok = ("Please find below the items recorded during Field review 2 (Electrical).\n\n"
               "EL-01 · Unit C, Upper Floor, Bath 2: wall behind toilet\nReceptacle beside the basin has no cover plate.\nSend to close: photo: completed\n\n"
               "EL-02 · Unit C, Upper Floor, Bath 2: wall behind toilet\nPanel schedule card is missing from the sub-panel door.\nSend to close: photo: completed\n\n"
               "Please reply with the evidence named above by [date].\n\nRow Houses site office")
    FakeFieldAgent.messages = [
        {"subject": "Field review 2", "body": body_ok.replace("EL-02", "EL-2")},                  # misses an item id
        {"subject": "Field review 2 (Electrical): 2 items", "body": body_ok.replace("Please reply", "Work is acceptable. Please reply")},  # judgement word
        {"subject": "Field review 2 (Electrical): 2 items", "body": body_ok.replace(" by [date]", " this week")},   # no date placeholder
        {"subject": "Field review 2 (Electrical): 2 items to close at Row Houses", "body": body_ok},
    ]
    r = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["error"] is None and out["package"]["count"] == 2 and out["package"]["photos"] == 2
    assert out["package"]["items"][0]["item_id"] == "EL-01" and out["package"]["items"][0]["sheet"] == "EL-2"
    assert out["message"]["review_id"] == rev["id"] and out["message"]["subject"].startswith("Field review 2 (Electrical)")
    assert "EL-02" in out["message"]["body"] and "[date]" in out["message"]["body"]
    prompt = " ".join(c["text"] for c in FakeFieldAgent.calls[-1] if "text" in c)
    assert "EL-01 | sheet EL-2" in prompt and "Panel schedule card" in prompt and "Row Houses" in prompt
    # the finished review carries its package; the draft shows on the Messages tab, tied to the review not an item
    p = client.get(f"/api/projects/{slug}").json()
    fin = next(x for x in p["reviews"] if x["id"] == rev["id"])
    assert fin["package"]["count"] == 2 and fin["status"] == "finished"
    assert p["messages"][0]["review_id"] == rev["id"] and p["messages"][0]["item_id"] == ""
    # finishing again does not draft twice; the redo route does
    assert client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish").json()["message"]["id"] == out["message"]["id"]
    FakeFieldAgent.messages = [{"subject": "Field review 2 (Electrical): second draft", "body": body_ok}]
    again = client.post(f"/api/projects/{slug}/reviews/{rev['id']}/message").json()["message"]
    assert again["id"] != out["message"]["id"] and again["subject"].endswith("second draft")
    assert len(client.get(f"/api/projects/{slug}").json()["messages"]) == 2
    # the agent failing does not block finishing
    FakeFieldAgent.messages = []
    rev3 = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "pin_x": 0.1, "pin_y": 0.1, "review_id": rev3["id"],
                "location": "Unit C, Upper Floor, Bath 2: ceiling", "description": "Light fixture hangs loose from the box.",
                "evidence_required": "photo: completed"})
    r = client.post(f"/api/projects/{slug}/reviews/{rev3['id']}/finish").json()
    assert r["review"]["status"] == "finished" and r["message"] is None and "could not draft" in r["error"]


def test_plan_views_come_from_grid_cells_and_are_validated(client, tmp_path):
    """The agent answers in grid cells; code turns them into fractions of the sheet and rejects bad answers."""
    slug, sid = _seed(client, tmp_path)

    # the sheet is a plan without views, so it is on the to-do list
    todo = client.get(f"/api/projects/{slug}/plans/todo").json()["sheets"]
    assert [t["id"] for t in todo] == [sid]

    FakeFieldAgent.proposals = [
        {"title": "MAIN FLOOR PLAN", "level": "Ground", "left_col": "A", "right_col": "D", "top_row": 1, "bottom_row": 6},   # not a project level
        {"title": "MAIN FLOOR PLAN", "level": "Main Floor", "left_col": "D", "right_col": "A", "top_row": 1, "bottom_row": 6},  # backwards
        {"title": "MAIN FLOOR PLAN", "level": "Main Floor", "left_col": "A", "right_col": "D", "top_row": 1, "bottom_row": 6},
        {"title": "UPPER FLOOR PLAN", "level": "Upper Floor", "left_col": "E", "right_col": "H", "top_row": 1, "bottom_row": 6},
    ]
    r = client.post(f"/api/projects/{slug}/sheets/{sid}/views")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["rejections"]) == 2
    assert [v["level"] for v in out["views"]] == ["Main Floor", "Upper Floor"]
    main = out["views"][0]
    assert (main["x"], main["y"]) == (0.0, 0.0) and abs(main["w"] - 4 / 12) < 1e-3 and abs(main["h"] - 6 / 8) < 1e-3
    assert abs(out["views"][1]["x"] - 4 / 12) < 1e-3
    # the agent saw the gridded sheet and the level names
    content = FakeFieldAgent.calls[-1]
    assert any("image" in c for c in content)
    assert "Main Floor, Upper Floor" in " ".join(c["text"] for c in content if "text" in c)
    # stored, shown on the project, and off the to-do list
    view = client.get(f"/api/projects/{slug}").json()
    sh = next(s for s in view["project"]["sheets"] if s["id"] == sid)
    assert [v["level"] for v in sh["views"]] == ["Main Floor", "Upper Floor"]
    assert client.get(f"/api/projects/{slug}/plans/todo").json()["sheets"] == []
    # the engineer can fix a box by hand
    r = client.put(f"/api/projects/{slug}/sheets/{sid}/views", json={"views": [{"title": "MAIN FLOOR PLAN", "level": "Main Floor", "x": 0.05, "y": 0.1, "w": 0.4, "h": 0.7}]})
    assert r.status_code == 200 and r.json()["views"][0]["source"] == "engineer"
    r = client.put(f"/api/projects/{slug}/sheets/{sid}/views", json={"views": [{"level": "Main Floor", "x": 0.8, "y": 0.1, "w": 0.4, "h": 0.7}]})
    assert r.status_code == 400
    # the agent records "none" on a sheet with no plan: no views, no error
    FakeFieldAgent.proposals = [{"title": "none", "level": ""}]
    assert client.post(f"/api/projects/{slug}/sheets/{sid}/views").json()["views"] == []


def test_grid_image_is_a_jpeg_of_the_sheet(tmp_path):
    from closeout.plans import grid_image
    img = tmp_path / "s.png"
    _png(img, (2400, 1600))
    data = grid_image(img)
    assert data[:2] == b"\xff\xd8"
    im = Image.open(io.BytesIO(data))
    assert max(im.size) <= 1568 and im.getpixel((im.width // 2, 1)) != (250, 250, 245)


def test_plan_boxes_carry_the_unit_and_crop_to_a_picture(client, tmp_path):
    """A box the reading marks with a unit label ties that floor plan to one of the project's units; a label that is
    not a unit of the building keeps the box for the whole building. Each box can be cut out as a picture."""
    slug, sid = _seed(client, tmp_path)
    FakeFieldAgent.proposals = [
        {"title": "MAIN FLOOR PLAN", "level": "Main Floor", "left_col": "A", "right_col": "D", "top_row": 1, "bottom_row": 6, "unit": "UNIT C"},
        {"title": "UPPER FLOOR PLAN", "level": "Upper Floor", "left_col": "E", "right_col": "H", "top_row": 1, "bottom_row": 6, "unit": "#9"},
    ]
    out = client.post(f"/api/projects/{slug}/sheets/{sid}/views").json()
    assert [v["unit"] for v in out["views"]] == ["Unit C", ""]
    assert out["views"][1]["unit_text"] == "#9" and len(out["notes"]) == 1 and out["rejections"] == []
    v = out["views"][0]
    r = client.get(f"/api/sheets/{sid}/crop", params={"x": v["x"], "y": v["y"], "w": v["w"], "h": v["h"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and len(r.content) > 100
    assert client.get(f"/api/sheets/{sid}/crop", params={"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}).status_code == 400
    # the engineer can re-tie a box to another unit by hand
    r = client.put(f"/api/projects/{slug}/sheets/{sid}/views", json={"views": [{**v, "unit": "", "source": "engineer"}]})
    assert r.status_code == 200 and r.json()["views"][0]["unit"] == "" and r.json()["views"][0]["source"] == "engineer"
    assert client.get(f"/api/projects/{slug}").json()["project"]["sheets"][0]["views"][0]["unit"] == ""
