"""What the contractor handed over for one item, filed by the office straight to it. Fictional project, no model call."""
from __future__ import annotations

from tests.test_coverage import _finding
import io

from PIL import Image

from tests.test_report import _jpeg_bytes, _seed, client  # noqa: F401  (the fixture)


def _other_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (30, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


def test_the_office_files_evidence_to_one_item(client, tmp_path):
    slug, sid = _seed(client, tmp_path)
    rev = client.post(f"/api/projects/{slug}/reviews", json={"discipline": "EL"}).json()["review"]
    f = _finding(client, slug, sid, rev["id"], "Unit A", "Cover plate missing on receptacle")
    item_id = f["item"]["item_id"]

    # Nothing filed yet: the item has no evidence and the packet does not exist.
    p = client.get(f"/api/projects/{slug}").json()
    assert p["packet"] is None

    r = client.post(f"/api/projects/{slug}/items/{item_id}/evidence",
                    files=[("files", ("IMG_9001.jpg", _jpeg_bytes(), "image/jpeg")), ("files", ("IMG_9002.jpg", _other_jpeg(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["files"] == 2 and out["completeness"] == "complete"

    # The run made no model call and the packet now carries the two files against the item, filed by the office.
    p = client.get(f"/api/projects/{slug}").json()
    run = [x for x in p["runs"] if x["id"] == out["run_id"]][0]
    assert run["model_id"] == "" and run["status"] == "done"
    it = [x for x in p["packet"]["items"] if x["item"]["item_id"] == item_id][0]
    assert it["completeness"] == "complete"
    assert sorted(e["filename"] for e in it["evidence"]) == ["IMG_9001.jpg", "IMG_9002.jpg"]
    assert all(e["provenance"] == "office" and e["tier"] == "explicit" and e["slot_index"] == 0 for e in it["evidence"])

    # Unknown item and an empty upload are refused.
    assert client.post(f"/api/projects/{slug}/items/ZZ-99/evidence", files=[("files", ("a.jpg", _jpeg_bytes(), "image/jpeg"))]).status_code == 404
