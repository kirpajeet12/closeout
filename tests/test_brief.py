"""Before the walk: the card is read from what the project holds. Fictional project, no model call."""
from __future__ import annotations

from closeout import revisions
from closeout.brief import field_brief
from tests.test_report import _jpeg_bytes, _seed, client  # noqa: F401  (the fixture)


def _doc(id_, rel, dated, pages, current=False, disc="EL"):
    return {"id": id_, "rel_path": rel, "dated": dated, "pages": pages, "kind": "drawing", "discipline": disc, "is_current": current}


def test_one_off_files_are_not_issues_of_the_set():
    docs = [
        _doc("a", "EL/240101/24-1_EL_Elm St_240101.pdf", "2024-01-01", 4),
        _doc("b", "EL/240301/24-1_EL_Elm St_240301.pdf", "2024-03-01", 4, current=True),
        _doc("b2", "PM/DD/Sent/24-1_EL_Elm St_240301.pdf", "2024-03-01", 4),               # a copy filed twice
        _doc("c", "EL/240201/24-1_EL_Elm St_240201-3(LOAD CALCULATION).pdf", "2024-02-01", 1),  # same name, one page
        _doc("d", "EL/240215/ELM STREET_CLOSET DETAIL.pdf", "2024-02-15", 1),
        _doc("e", "EL/240220/459-U07-01178 Redline.pdf", "2024-02-20", 4),                  # right size, different name
        _doc("p", "PL/240101/24-1_PL_Elm St_240101.pdf", "2024-01-01", 1, current=True, disc="PL"),
    ]
    assert revisions.set_members(docs, "EL") == {"a", "b", "b2"}
    kinds = revisions.issues_by_kind(docs, "EL")
    assert [i["dated"] for i in kinds["set"]] == ["2024-01-01", "2024-03-01"]
    assert sorted(i["file"] for i in kinds["other"]) == ["24-1_EL_Elm St_240201-3(LOAD CALCULATION).pdf", "459-U07-01178 Redline.pdf", "ELM STREET_CLOSET DETAIL.pdf"]
    assert revisions.set_members(docs, "PL") == {"p"} and revisions.set_members(docs, "AR") == set()


def test_brief_reads_the_set_open_items_and_missing_documents(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    p = client.get(f"/api/projects/{slug}").json()
    docs = p["project"]["documents"]
    assert all(d["in_set"] for d in docs if d["kind"] == "drawing")

    # an earlier walk left one item open and one accepted
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    for n, what in enumerate(["Smoke alarm missing at the top of the stair.", "Panel cover not fixed."]):
        r = client.post(f"/api/projects/{slug}/findings", data={"sheet_id": sid, "review_id": rev["id"], "location": "Unit C, Upper Floor, hall",
                        "description": what, "evidence_required": "photo: fixed", "unit": "Unit C", "level": "Upper Floor"},
                        files={"photo": (f"IMG_{n}.jpg", _jpeg_bytes(), "image/jpeg")})
        assert r.status_code == 200, r.text
    assert client.post(f"/api/projects/{slug}/reviews/{rev['id']}/finish").status_code == 200
    assert client.post(f"/api/projects/{slug}/items/EL-02/decision", json={"decision": "accept", "note": "Seen fixed."}).status_code == 200

    calls = []
    def fake_compare(old, new):
        calls.append((old["id"], new["id"]))
        return {"added": [], "removed": [], "renumbered": [], "unchanged": [{"number": "EL-1"}],
                "changed": [{"number": "EL-2", "title": "UPPER FLOOR POWER PLAN", "alike": 0.8, "now_says": ["ADDED HOUSE PANEL", "HOUSE", "PANEL H", "SEE SLD"], "no_longer_says": ["PANEL X"]}],
                "who_else": [{"discipline": "AR", "name": "Architectural", "dated": "2025-09-24", "file": "a.pdf"}], "basis": "words"}
    from closeout.store import Store
    st = Store(tmp_path / "data" / "closeout.db")
    pid = [x for x in st.projects() if x["slug"] == slug][0]["id"]
    st.set_docs_review(pid, {"summary": "", "missing": [{"what": "Panel schedule", "why": "Not in the set.", "discipline": "EL", "building": ""},
                                                        {"what": "Structural drawings", "why": "", "discipline": "", "building": ""}], "questions": []})
    b = field_brief(st, pid, "EL", compare=fake_compare)
    assert b["current"]["dated"] == "2026-03-01" and b["previous"]["dated"] == "2026-01-01" and b["issues"] == 2 and b["other_on_file"] == []
    assert calls == [(b["previous"]["id"], b["current"]["id"])]
    c = b["changes"]
    assert c["changed"] == 1 and c["unchanged"] == 1 and c["sheets"][0]["number"] == "EL-2"
    assert c["sheets"][0]["now_says"] == ["ADDED HOUSE PANEL", "HOUSE", "PANEL H"] and c["sheets"][0]["more"] == 2
    assert c["who_else"][0]["discipline"] == "AR"
    assert [o["item_id"] for o in b["open_items"]] == ["EL-01"] and b["open_items"][0]["status"] == "open" and b["open_items"][0]["review"] == "Field review 1"
    assert b["last_review"]["title"] == "Field review 1" and b["last_review"]["count"] == 2 and b["reviews_done"] == 1
    assert [m["what"] for m in b["missing"]] == ["Panel schedule"] and b["general_missing"] == 1
    assert b["party"] is None or b["party"]["role"]

    # through the route: the compare reads the real files in the seeded folder, or reports why it could not
    r = client.get(f"/api/projects/{slug}/field/el/brief")
    assert r.status_code == 200
    j = r.json()
    assert j["discipline"] == "EL" and j["current"]["dated"] == "2026-03-01" and [o["item_id"] for o in j["open_items"]] == ["EL-01"]
    assert j["changes"] is not None
    # a discipline with nothing on file still answers
    j = client.get(f"/api/projects/{slug}/field/ME/brief").json()
    assert j["current"] is None and j["changes"] is None and j["open_items"] == [] and j["missing"] == []
