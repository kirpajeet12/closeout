"""The live site needs the office code once; contractor links never do."""
from fastapi.testclient import TestClient

from closeout.api import create_app
from closeout.config import Settings


def _client(tmp_path, code):
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", model_id="fake", access_code=code)))


def test_without_a_code_the_app_is_open(tmp_path):
    c = _client(tmp_path, "")
    assert c.get("/api/projects").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200


def test_the_office_code_gates_screens_and_api_but_not_contractor_links(tmp_path):
    c = _client(tmp_path, "north-shore")
    assert c.get("/api/projects").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    assert c.get("/signin").status_code == 200
    assert c.get("/api/c/not-a-token").status_code == 404          # reached the route, no gate
    assert c.get("/c/not-a-token").status_code == 200
    r = c.post("/signin", data={"code": "wrong"}, follow_redirects=False)
    assert r.status_code == 403 and "did not match" in r.text and "closeout_access" not in r.cookies
    r = c.post("/signin", data={"code": " north-shore "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and r.cookies.get("closeout_access")
    assert c.get("/api/projects").status_code == 200
    assert c.get("/signin", follow_redirects=False).status_code == 303
    c.post("/signout", follow_redirects=False)
    assert c.get("/api/projects").status_code == 401
