"""The office's Gmail, connected once with Google's sign-in. Closeout sends from that address and reads only two kinds
of mail: replies in threads it sent, and messages the engineer (or their own assistant) labels for it. Tokens live in
the local database, never in source or logs."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlencode

import httpx

from .config import Settings

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


def configured(settings: Settings) -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def auth_url(settings: Settings, redirect_uri: str, state: str) -> str:
    q = {"client_id": settings.google_client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "access_type": "offline", "prompt": "consent", "state": state}
    return AUTH + "?" + urlencode(q)


def exchange_code(settings: Settings, code: str, redirect_uri: str) -> dict:
    """The one-time code from Google's consent screen becomes a refresh token. Returns {refresh_token, access_token}."""
    r = httpx.post(TOKEN, data={"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    r.raise_for_status()
    return r.json()


def access_token(settings: Settings, refresh_token: str) -> str:
    r = httpx.post(TOKEN, data={"refresh_token": refresh_token, "client_id": settings.google_client_id,
                                "client_secret": settings.google_client_secret, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


@dataclass
class Incoming:
    """One message read from the mailbox, reduced to what filing needs."""
    id: str
    thread_id: str
    from_addr: str
    subject: str
    text: str
    sent_at: str = ""
    files: list[tuple[str, bytes]] = field(default_factory=list)
    from_me: bool = False


def parse_raw(raw: bytes, own_address: str = "") -> Incoming:
    """Reduce a raw RFC 822 message: plain text body (html stripped when that is all there is) and the attachments."""
    m = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(m.get("From", ""))[1]
    text = ""
    body = m.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = re.sub(r"<[^>]+>", " ", content)
        text = re.sub(r"[ \t]+", " ", content).strip()
    files: list[tuple[str, bytes]] = []
    for part in m.iter_attachments():
        name = part.get_filename() or ""
        data = part.get_payload(decode=True)
        if name and data:
            files.append((name, data))
    return Incoming(id="", thread_id="", from_addr=sender, subject=m.get("Subject", "") or "", text=text,
                    sent_at=m.get("Date", "") or "", files=files, from_me=bool(own_address) and sender.lower() == own_address.lower())


class Gmail:
    """Thin client over the Gmail REST API for one connected account."""

    def __init__(self, settings: Settings, refresh_token: str, address: str = ""):
        self.settings = settings
        self.refresh_token = refresh_token
        self.address = address
        self._token = ""

    def _headers(self) -> dict:
        if not self._token:
            self._token = access_token(self.settings, self.refresh_token)
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, **params) -> dict:
        r = httpx.get(f"{API}/{path}", headers=self._headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = httpx.post(f"{API}/{path}", headers=self._headers(), json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def profile(self) -> str:
        return self._get("profile").get("emailAddress", "")

    def send(self, to: str, subject: str, body: str) -> dict:
        """Send one plain-text message from the connected address. Returns {id, threadId}."""
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self.address
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        out = self._post("messages/send", {"raw": raw})
        return {"id": out.get("id", ""), "thread_id": out.get("threadId", "")}

    def search(self, query: str, limit: int = 50) -> list[str]:
        out = self._get("messages", q=query, maxResults=limit)
        return [m["id"] for m in out.get("messages", [])]

    def thread_message_ids(self, thread_id: str) -> list[str]:
        out = self._get(f"threads/{thread_id}", format="minimal")
        return [m["id"] for m in out.get("messages", [])]

    def message(self, message_id: str) -> Incoming:
        out = self._get(f"messages/{message_id}", format="raw")
        raw = base64.urlsafe_b64decode(out["raw"] + "=" * (-len(out["raw"]) % 4))
        inc = parse_raw(raw, self.address)
        inc.id, inc.thread_id = out.get("id", message_id), out.get("threadId", "")
        return inc

    def label_id(self, name: str) -> str:
        """The id of a label, created when missing."""
        for lb in self._get("labels").get("labels", []):
            if lb.get("name", "").lower() == name.lower():
                return lb["id"]
        return self._post("labels", {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})["id"]

    def mark(self, message_id: str, add: str, remove: str = "") -> None:
        body: dict = {"addLabelIds": [self.label_id(add)]}
        if remove:
            body["removeLabelIds"] = [self.label_id(remove)]
        self._post(f"messages/{message_id}/modify", body)
