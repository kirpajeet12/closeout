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


def test_with_only_the_code_the_sign_in_page_asks_for_the_code_first(tmp_path):
    page = _client(tmp_path, "north-shore").get("/signin").text
    assert 'name="code"' in page and 'name="email"' not in page and "Continue with Google" not in page


def _link(text):
    import re
    return re.search(r"/set-password/[\w-]+", text).group(0)


def test_accounts_welcome_link_sign_in_sign_out_and_forgot_password(tmp_path, monkeypatch):
    from closeout import mail as mail_mod
    outbox = []
    monkeypatch.setattr(mail_mod, "can_send", lambda s: True)
    monkeypatch.setattr(mail_mod, "send_email", lambda s, to, subject, body, attachments=(): outbox.append((to, subject, body)) or "id")
    c = _client(tmp_path, "north-shore")
    c.post("/signin", data={"code": "north-shore"})
    r = c.post("/api/users", json={"email": "Site.Lead@Example.com", "name": "Sam Lee"})
    assert r.status_code == 200 and r.json()["emailed"] and r.json()["link"] == ""
    to, subject, body = outbox[-1]
    assert to == "site.lead@example.com" and "Your Closeout account" in subject and "Hello Sam," in body
    assert c.post("/api/users", json={"email": "site.lead@example.com"}).status_code == 409
    welcome = _link(body)

    fresh = _client(tmp_path, "north-shore")
    assert "has no password yet" in fresh.post("/signin", data={"email": "site.lead@example.com", "password": "x"}).text
    assert fresh.post(welcome, data={"password": "short", "confirm": "short"}).status_code == 400
    assert fresh.post(welcome, data={"password": "long enough pw", "confirm": "different pw!"}).status_code == 400
    r = fresh.post(welcome, data={"password": "long enough pw", "confirm": "long enough pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.cookies.get("closeout_session")
    assert fresh.get("/api/me").json()["user"]["email"] == "site.lead@example.com"
    assert fresh.get(welcome).status_code == 410                                     # used once
    fresh.post("/signout", follow_redirects=False)
    assert fresh.get("/api/projects").status_code == 401

    assert fresh.post("/signin", data={"email": "site.lead@example.com", "password": "wrong password"}).status_code == 403
    r = fresh.post("/signin", data={"email": "SITE.LEAD@example.com", "password": "long enough pw"}, follow_redirects=False)
    assert r.status_code == 303 and fresh.get("/api/projects").status_code == 200
    fresh.post("/signout", follow_redirects=False)

    n = len(outbox)
    r = fresh.post("/forgot", data={"email": "nobody@example.com"})
    assert r.status_code == 200 and "Check your email" in r.text and len(outbox) == n    # same answer, nothing sent
    fresh.post("/forgot", data={"email": "site.lead@example.com"})
    assert len(outbox) == n + 1 and "Reset your Closeout password" in outbox[-1][1]
    reset = _link(outbox[-1][2])
    fresh.post(reset, data={"password": "a brand new one", "confirm": "a brand new one"})
    assert fresh.get("/api/projects").status_code == 200
    fresh.post("/signout", follow_redirects=False)
    assert fresh.post("/signin", data={"email": "site.lead@example.com", "password": "long enough pw"}).status_code == 403
    assert fresh.post("/signin", data={"email": "site.lead@example.com", "password": "a brand new one"}, follow_redirects=False).status_code == 303


def test_without_email_the_office_gets_the_link_to_pass_on_and_can_remove_people(tmp_path):
    c = _client(tmp_path, "north-shore")
    c.post("/signin", data={"code": "north-shore"})
    j = c.post("/api/users", json={"email": "pm@example.com"}).json()
    assert not j["emailed"] and "/set-password/" in j["link"]
    uid = j["user"]["id"]
    assert c.get(j["link"].replace("http://testserver", "")).status_code == 200
    assert c.post(f"/api/users/{uid}/link").json()["link"] != j["link"]
    assert c.get(j["link"].replace("http://testserver", "")).status_code == 410        # the newer link replaces it
    assert c.delete(f"/api/users/{uid}").json()["users"] == []


def test_stored_secrets_are_hashes_only(tmp_path):
    import sqlite3
    c = _client(tmp_path, "north-shore")
    c.post("/signin", data={"code": "north-shore"})
    link = c.post("/api/users", json={"email": "pm@example.com"}).json()["link"]
    token = link.rsplit("/", 1)[1]
    c.post(link.replace("http://testserver", ""), data={"password": "long enough pw", "confirm": "long enough pw"})
    db = sqlite3.connect(tmp_path / "data" / "closeout.db")
    dump = "\n".join(db.iterdump())
    assert token not in dump and "long enough pw" not in dump and "scrypt$" in dump


def test_report_an_issue(tmp_path):
    c = _client(tmp_path, "north-shore")
    c.post("/signin", data={"code": "north-shore"})
    assert c.post("/api/issues", json={"what": "no"}).status_code == 400
    j = c.post("/api/issues", json={"what": "The plan did not open on my phone", "page": "#/p/x/field"}).json()
    iid = j["issue"]["id"]
    assert j["issue"]["reported_by"] == "office code" and j["issues"][0]["status"] == "open"
    assert c.post(f"/api/issues/{iid}", json={"status": "fixed"}).json()["issues"][0]["status"] == "fixed"


def test_too_many_wrong_tries_are_slowed(tmp_path):
    c = _client(tmp_path, "north-shore")
    for _ in range(8):
        c.post("/signin", data={"code": "wrong"})
    assert c.post("/signin", data={"code": "north-shore"}).status_code == 429


def _id_token(claims):
    import base64, json
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


def test_continue_with_google_signs_in_only_people_the_office_added(tmp_path, monkeypatch):
    import httpx
    from urllib.parse import parse_qs, urlparse
    from closeout import signin as signin_mod
    cid = "office.apps.googleusercontent.com"
    back = {"claims": {}}
    monkeypatch.setattr(signin_mod.httpx, "post", lambda url, data, timeout: httpx.Response(200, json={"id_token": _id_token(back["claims"])},
                                                                                           request=httpx.Request("POST", url)))
    s = Settings(data_dir=tmp_path / "data", model_id="fake", access_code="north-shore", google_client_id=cid, google_client_secret="secret-value")
    office = TestClient(create_app(s))
    page = office.get("/signin").text
    assert "Continue with Google" in page and "Continue with Microsoft" not in page
    assert office.get("/signin/microsoft", follow_redirects=False).headers["location"] == "/signin"
    assert office.get("/api/mail/callback?code=x&state=anything").status_code == 401      # connecting the mailbox stays behind the gate

    def start(c):
        r = c.get("/signin/google", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["scope"] == ["openid email"] and q["redirect_uri"][0].endswith("/api/mail/callback")
        return q["state"][0], q["nonce"][0]

    person = TestClient(create_app(s))
    state, nonce = start(person)
    back["claims"] = {"aud": cid, "nonce": nonce, "email": "Site.Lead@Example.com", "email_verified": True}
    r = person.get(f"/api/mail/callback?code=c1&state={state}", follow_redirects=False)
    assert r.status_code == 403 and "Ask the office to add you" in r.text and not r.cookies.get("closeout_session")

    office.post("/signin", data={"code": "north-shore"})
    assert office.post("/api/users", json={"email": "site.lead@example.com"}).status_code == 200

    start(person)
    assert person.get("/api/mail/callback?code=c2&state=signin.forged", follow_redirects=False).status_code == 400
    state, nonce = start(person)
    back["claims"] = {"aud": cid, "nonce": nonce, "email": "site.lead@example.com", "email_verified": False}
    assert person.get(f"/api/mail/callback?code=c3&state={state}", follow_redirects=False).status_code == 403
    state, nonce = start(person)
    back["claims"] = {"aud": "someone-else", "nonce": nonce, "email": "site.lead@example.com", "email_verified": True}
    assert person.get(f"/api/mail/callback?code=c4&state={state}", follow_redirects=False).status_code == 403
    state, nonce = start(person)
    back["claims"] = {"aud": cid, "nonce": nonce, "email": "site.lead@example.com", "email_verified": True}
    r = person.get(f"/api/mail/callback?code=c5&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and r.cookies.get("closeout_session")
    assert person.get("/api/me").json()["user"]["email"] == "site.lead@example.com"

    stranger = TestClient(create_app(s))
    state, _ = start(stranger)
    stranger.cookies.clear()
    assert stranger.get(f"/api/mail/callback?code=c6&state={state}", follow_redirects=False).status_code == 400   # no cookie: not this browser


def test_microsoft_sign_in_needs_a_confirmed_address(tmp_path, monkeypatch):
    import httpx
    from urllib.parse import parse_qs, urlparse
    from closeout import signin as signin_mod
    back = {"claims": {}}
    monkeypatch.setattr(signin_mod.httpx, "post", lambda url, data, timeout: httpx.Response(200, json={"id_token": _id_token(back["claims"])},
                                                                                           request=httpx.Request("POST", url)))
    s = Settings(data_dir=tmp_path / "data", model_id="fake", access_code="north-shore", microsoft_client_id="ms-app", microsoft_client_secret="ms-secret")
    office = TestClient(create_app(s))
    assert "Continue with Microsoft" in office.get("/signin").text and "Continue with Google" not in office.get("/signin").text
    office.post("/signin", data={"code": "north-shore"})
    office.post("/api/users", json={"email": "pm@builder.example"})
    c = TestClient(create_app(s))

    def start():
        r = c.get("/signin/microsoft", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert r.headers["location"].startswith("https://login.microsoftonline.com/") and q["redirect_uri"][0].endswith("/api/mail/callback/microsoft")
        return q["state"][0], q["nonce"][0]

    state, nonce = start()
    back["claims"] = {"aud": "ms-app", "nonce": nonce, "preferred_username": "pm@builder.example"}          # no email claim
    assert c.get(f"/api/mail/callback/microsoft?code=a&state={state}", follow_redirects=False).status_code == 403
    state, nonce = start()
    back["claims"] = {"aud": "ms-app", "nonce": nonce, "email": "pm@builder.example", "xms_edov": False}
    assert c.get(f"/api/mail/callback/microsoft?code=b&state={state}", follow_redirects=False).status_code == 403
    state, nonce = start()
    back["claims"] = {"aud": "ms-app", "nonce": nonce, "email": "PM@builder.example"}
    r = c.get(f"/api/mail/callback/microsoft?code=c&state={state}", follow_redirects=False)
    assert r.status_code == 303 and c.get("/api/me").json()["user"]["email"] == "pm@builder.example"
