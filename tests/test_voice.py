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
    assert cl.get("/api/voice").json() == {"available": False, "live": False}
    pid = cl.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    assert cl.post(f"/api/projects/{pid}/speak", json={"text": "hello"}).status_code == 404


def test_with_a_key_the_answer_comes_back_as_audio(tmp_path, monkeypatch):
    cl = _client(tmp_path, "test-key")
    assert cl.get("/api/voice").json() == {"available": True, "live": True}
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


OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def test_the_spoken_conversation_is_opened_by_the_server_with_the_agent_as_the_brain(tmp_path, monkeypatch):
    """The browser only sends its connection offer. The server adds the key, the voice, the instructions that hand
    every project question back to Closeout, and returns the answer offer plus the session id."""
    cl = _client(tmp_path, "test-key")
    pid = cl.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, auth=kw["headers"]["authorization"], body=kw["json"])
        return httpx.Response(201, json={"session": {"id": "live_123"}, "transport": {"type": "webrtc", "sdp": "v=0 answer"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(api_mod.httpx, "post", fake_post)
    r = cl.post(f"/api/projects/{pid}/live/session", json={"sdp": OFFER})
    assert r.status_code == 201 and r.json() == {"session_id": "live_123", "sdp": "v=0 answer"}
    assert seen["url"] == "https://api.openai.com/v1/live/sessions" and seen["auth"] == "Bearer test-key"
    session, transport = seen["body"]["session"], seen["body"]["transport"]
    assert session["model"] == "gpt-live-1" and session["delegation"] == {"type": "client"}
    assert session["audio"] == {"output": {"voice": "marin"}}
    assert "Row Houses" in session["instructions"] and "delegate" in session["instructions"].lower()
    assert "never confirm" in session["instructions"].lower()
    assert transport == {"type": "webrtc", "sdp": OFFER}
    assert "test-key" not in r.text

    assert cl.post(f"/api/projects/{pid}/live/session", json={"sdp": "not an offer"}).status_code == 400
    monkeypatch.setattr(api_mod.httpx, "post", lambda url, **kw: httpx.Response(401, request=httpx.Request("POST", url)))
    assert cl.post(f"/api/projects/{pid}/live/session", json={"sdp": OFFER}).status_code == 502


def test_without_a_key_there_is_no_spoken_conversation(tmp_path):
    cl = _client(tmp_path, "")
    pid = cl.post("/api/projects/blank", json={"name": "Row Houses"}).json()["slug"]
    assert cl.post(f"/api/projects/{pid}/live/session", json={"sdp": OFFER}).status_code == 404
