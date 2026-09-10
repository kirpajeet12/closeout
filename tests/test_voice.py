"""The natural voice: off without a key, and when on, text goes out and audio comes back. The key never reaches the browser."""
import httpx
from fastapi.testclient import TestClient

from closeout import api as api_mod
from closeout.config import Settings


def _client(tmp_path, key):
    s = Settings(data_dir=tmp_path / "data", voice_key=key)
    return TestClient(api_mod.create_app(s))


def test_without_a_key_the_phone_voice_is_used(tmp_path):
    cl = _client(tmp_path, "")
    assert cl.get("/api/voice").json() == {"available": False}
    pid = cl.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    assert cl.post(f"/api/projects/{pid}/speak", json={"text": "hello"}).status_code == 404


def test_with_a_key_the_answer_comes_back_as_audio(tmp_path, monkeypatch):
    cl = _client(tmp_path, "test-key")
    assert cl.get("/api/voice").json() == {"available": True}
    pid = cl.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, auth=kw["headers"]["authorization"], body=kw["json"])
        return httpx.Response(200, content=b"ID3fake-mp3", request=httpx.Request("POST", url))

    monkeypatch.setattr(api_mod.httpx, "post", fake_post)
    r = cl.post(f"/api/projects/{pid}/speak", json={"text": "  EL-01 is   still open. "})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg") and r.content == b"ID3fake-mp3"
    assert seen["auth"] == "Bearer test-key" and seen["body"]["input"] == "EL-01 is still open." and seen["body"]["voice"] == "marin"
    assert cl.post(f"/api/projects/{pid}/speak", json={"text": "   "}).status_code == 400
    monkeypatch.setattr(api_mod.httpx, "post", lambda url, **kw: httpx.Response(401, request=httpx.Request("POST", url)))
    assert cl.post(f"/api/projects/{pid}/speak", json={"text": "hi"}).status_code == 502
