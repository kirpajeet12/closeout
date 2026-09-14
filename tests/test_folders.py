"""The engineer's own folders in the Documents tab: made anywhere in the project folder, nested, renamed, filed into, removed."""
from __future__ import annotations

from closeout.store import Store
from tests.test_review import FakeFieldAgent, _seed, client  # noqa: F401  (fixture)


def _project(client, tmp_path):
    slug, _ = _seed(client, tmp_path)
    st = Store(client.settings.data_dir / "closeout.db")
    pid = st.project_by_slug(slug)["id"]
    st.replace_documents(pid, [{"rel_path": "Electrical/EL.pdf", "discipline": "EL", "dated": "2026-01-01", "pages": 1, "kind": "drawing",
                                "is_current": 1, "sha256": "x", "size": 1},
                               {"rel_path": "PM/Permit.pdf", "discipline": "PM", "dated": None, "pages": 1, "kind": "document",
                                "is_current": 0, "sha256": "y", "size": 1}])
    return slug, st, pid


def test_folders_nest_anywhere_in_the_project_folder_and_files_can_be_filed_into_them(client, tmp_path):
    slug, st, pid = _project(client, tmp_path)
    url = f"/api/projects/{slug}/folders"
    r = client.post(url, json={"parent": "site/EL", "name": "  Shop   drawings "})
    assert r.status_code == 200, r.text
    shop = r.json()["folder"]
    assert shop["name"] == "Shop drawings" and shop["parent"] == "site/EL"
    inner = client.post(url, json={"parent": "u/" + shop["id"], "name": "Panel schedules"}).json()["folder"]
    deeper = client.post(url, json={"parent": "u/" + inner["id"], "name": "Rev 2"}).json()["folder"]
    assert client.post(url, json={"parent": "b/Unit C", "name": "Inspections"}).status_code == 200
    assert client.post(url, json={"parent": "prj", "name": "Correspondence"}).status_code == 200
    assert [f["name"] for f in client.get(f"/api/projects/{slug}").json()["folders"]] == \
        ["Shop drawings", "Panel schedules", "Rev 2", "Inspections", "Correspondence"]

    r = client.post(f"/api/projects/{slug}/filing", json={"file": "Permit.pdf", "folder": deeper["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["filings"]["Permit.pdf"]["folder"] == deeper["id"]
    assert client.post(f"/api/projects/{slug}/filing", json={"file": "Permit.pdf", "folder": "fld_nope"}).status_code == 400
    # filled folders stay; so do folders with folders inside
    assert client.delete(f"{url}/{deeper['id']}").status_code == 400
    assert client.delete(f"{url}/{inner['id']}").status_code == 400
    assert client.post(f"/api/projects/{slug}/filing/undo", json={"file": "Permit.pdf"}).status_code == 200
    assert st.filings(pid) == {}
    assert client.delete(f"{url}/{deeper['id']}").status_code == 200
    assert client.patch(f"{url}/{inner['id']}", json={"name": "Panels"}).json()["folders"][1]["name"] == "Panels"


def test_a_folder_needs_a_real_place_a_name_and_no_twin(client, tmp_path):
    slug, _, _ = _project(client, tmp_path)
    url = f"/api/projects/{slug}/folders"
    assert client.post(url, json={"parent": "crp", "name": "Mine"}).status_code == 400          # the occupancy checklist is fixed
    assert client.post(url, json={"parent": "b/Nowhere", "name": "Mine"}).status_code == 400
    assert client.post(url, json={"parent": "u/fld_missing", "name": "Mine"}).status_code == 400
    assert client.post(url, json={"parent": "site", "name": "   "}).status_code == 400
    assert client.post(url, json={"parent": "site", "name": "a/b"}).status_code == 400
    one = client.post(url, json={"parent": "site", "name": "Permits"}).json()["folder"]
    assert client.post(url, json={"parent": "site", "name": "permits"}).status_code == 400
    two = client.post(url, json={"parent": "prj", "name": "Permits"}).json()["folder"]            # same name, another place
    assert client.patch(f"{url}/{two['id']}", json={"name": "Site permits"}).status_code == 200
    assert client.patch(f"{url}/nope", json={"name": "x"}).status_code == 404
    assert client.delete(f"{url}/{one['id']}").status_code == 200
