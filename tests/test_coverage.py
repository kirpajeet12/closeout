"""Stages and units: which walks are done, which units each walk covered. Fictional project, no model call."""
from __future__ import annotations

from closeout import coverage
from tests.test_report import _jpeg_bytes, _seed, client  # noqa: F401  (the fixture)


def _finding(client, slug, sid, rev_id, unit, what):
    r = client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "review_id": rev_id, "location": f"{unit}, Upper Floor",
                    "description": what, "evidence_required": "photo: fixed", "unit": unit, "level": "Upper Floor"},
                    files={"photo": ("IMG_1.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def test_buildings_and_default_stages():
    prj = {"model": {"units": [{"label": "Unit A (#1 – 12 Elm St)", "address": "#1 12 Elm St"}, {"label": "Unit B (#2 – 12 Elm St)", "address": "#2 12 Elm St"},
                               {"label": "Unit C (#1 – 14 Elm St)", "address": "#1 14 Elm St"}]}, "stages": {}}
    assert [(b["name"], b["units"]) for b in coverage.buildings(prj)] == [("12 Elm St", ["Unit A (#1 – 12 Elm St)", "Unit B (#2 – 12 Elm St)"]), ("14 Elm St", ["Unit C (#1 – 14 Elm St)"])]
    assert coverage.stages_for(prj, "EL") == ["Underground / slab", "Rough-in", "Pre-drywall", "Final"]
    assert coverage.stages_for({**prj, "stages": {"EL": ["Rough-in", "Final"]}}, "EL") == ["Rough-in", "Final"]
    assert coverage.stages_for(prj, "PM") == ["Rough-in", "Final"]
    assert coverage.short_unit("Unit B (#2 – 12 Elm St)") == "Unit B"


def _three_units(tmp_path, slug):
    from closeout.store import Store
    st = Store(tmp_path / "data" / "closeout.db")
    prj = [x for x in st.projects() if x["slug"] == slug][0]
    st.set_project_model(prj["id"], {**prj["model"], "units": [
        {"label": "Unit A (#1 – 12 Elm St)", "address": "#1 12 Elm St", "levels": ["Main Floor", "Upper Floor"]},
        {"label": "Unit B (#2 – 12 Elm St)", "address": "#2 12 Elm St", "levels": ["Main Floor", "Upper Floor"]},
        {"label": "Unit C (#1 – 14 Elm St)", "address": "#1 14 Elm St", "levels": ["Main Floor", "Upper Floor"]}]})


def test_a_walk_knows_its_stage_and_the_units_it_covered(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    _three_units(tmp_path, slug)
    p = client.get(f"/api/projects/{slug}").json()
    units = [u for b in p["units"] for u in b["units"]]
    assert [b["name"] for b in p["units"]] == ["12 Elm St", "14 Elm St"] and len(units) == 3
    assert p["stages"]["EL"] == ["Underground / slab", "Rough-in", "Pre-drywall", "Final"]

    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL", "stage": "Rough-in"}).json()["review"]
    assert rev["stage"] == "Rough-in" and rev["units"] == [] and rev["units_edited"] is False
    _finding(client, slug, sid, rev["id"], units[0], "Box not fixed to the stud.")
    _finding(client, slug, sid, rev["id"], units[0], "Cable unsupported over the door.")
    _finding(client, slug, sid, rev["id"], units[1], "Missing nail plate at the plumbing wall.")
    rv = next(r for r in client.get(f"/api/projects/{slug}").json()["reviews"] if r["id"] == rev["id"])
    assert rv["units"] == [units[0], units[1]] and rv["units_auto"] == [units[0], units[1]]

    # the reviewer says a third unit was walked too, with nothing to record
    r = client.patch(f"/api/projects/{slug}/reviews/{rev['id']}", json={"units": [units[2], units[0], units[1]]})
    assert r.status_code == 200 and r.json()["review"]["units"] == units[:3] and r.json()["review"]["units_edited"] is True
    assert client.patch(f"/api/projects/{slug}/reviews/{rev['id']}", json={"units": ["Unit Z"]}).status_code == 400
    assert client.patch(f"/api/projects/{slug}/reviews/nope", json={"units": []}).status_code == 404

    cov = client.get(f"/api/projects/{slug}/field/EL/coverage").json()
    rough = next(s for s in cov["stage_state"] if s["stage"] == "Rough-in")
    assert rough["walks"] == 1 and rough["units_covered"] == 3 and rough["started"] and not rough["done"]
    rows = {u["label"]: u for b in cov["buildings"] for u in b["units"]}
    assert rows[units[0]]["cells"]["Rough-in"][0]["items"] == 2 and rows[units[0]]["open"] == 2
    assert rows[units[2]]["cells"]["Rough-in"][0]["items"] == 0 and rows[units[2]]["cells"]["Final"] == []
    assert rows[units[1]]["items"] == 1

    # the report says which units were walked and groups the items by unit
    assert client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish").status_code == 200
    rep = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/report.json").json()
    assert rep["review"]["stage"] == "Rough-in" and rep["units_edited"] is True
    assert rep["walked"] == [coverage.short_unit(u) for u in units[:3]]
    assert all(u not in rep["not_walked"] for u in rep["walked"])
    html = client.get(f"/api/projects/{slug}/reviews/{rev['id']}/report").text
    assert "Units walked" in html and "As edited by the reviewer." in html and "Rough-in" in html
    assert html.index(coverage.short_unit(units[0])) < html.index("Box not fixed to the stud.")

    # back to what the deficiencies say
    r = client.patch(f"/api/projects/{slug}/reviews/{rev['id']}", json={"units_reset": True})
    assert r.json()["review"]["units"] == units[:2] and r.json()["review"]["units_edited"] is False
    # accepting the items empties the open count, the stage stays walked
    for item in ("EL-01", "EL-02"):
        assert client.post(f"/api/projects/{slug}/items/{item}/decision", json={"decision": "accept", "note": "Seen fixed."}).status_code == 200
    cov = client.get(f"/api/projects/{slug}/field/EL/coverage").json()
    rows = {u["label"]: u for b in cov["buildings"] for u in b["units"]}
    assert rows[units[0]]["open"] == 0 and rows[units[0]]["items"] == 2


def test_the_office_can_set_its_own_stage_list(client, tmp_path):
    slug, _sid = _seed(client, tmp_path)
    r = client.patch(f"/api/projects/{slug}/stages", json={"discipline": "el", "stages": [" Rough-in ", "", "Final"]})
    assert r.status_code == 200 and r.json() == {"discipline": "EL", "stages": ["Rough-in", "Final"]}
    assert client.get(f"/api/projects/{slug}").json()["stages"]["EL"] == ["Rough-in", "Final"]
    assert client.patch(f"/api/projects/{slug}/stages", json={"discipline": "EL", "stages": []}).status_code == 400
    assert client.patch(f"/api/projects/{slug}/stages", json={"discipline": "ZZ", "stages": ["Final"]}).status_code == 400
    # a walk on a stage that is not on the list still gets its own column
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL", "stage": "Service entrance"}).json()["review"]
    cov = client.get(f"/api/projects/{slug}/field/EL/coverage").json()
    assert cov["stages"] == ["Rough-in", "Final", "Service entrance"] and cov["unstaged"] == []
    assert client.patch(f"/api/projects/{slug}/reviews/{rev['id']}", json={"stage": ""}).status_code == 200
    assert client.get(f"/api/projects/{slug}/field/EL/coverage").json()["unstaged"] == [rev["id"]]
