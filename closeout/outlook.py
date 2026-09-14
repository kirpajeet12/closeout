"""The office's Microsoft 365 or Outlook mailbox, connected once with Microsoft's sign-in. Business Microsoft mailboxes no
longer accept a password from an app, so this goes through Microsoft Graph instead of IMAP. Closeout sends from that
address and reads only replies to what it sent and mail whose subject carries a review's reference, both in the inbox;
it reads without marking anything as read. Tokens live in the local database, never in source or logs."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlencode

import httpx

from .config import Settings
from .gmail import Incoming, parse_raw, summary, view_raw
from .mailbox import REFS, MailError

SCOPE = "offline_access openid email User.Read Mail.ReadWrite Mail.Send"
LOGIN = "https://login.microsoftonline.com"
API = "https://graph.microsoft.com/v1.0/me"
INLINE_LIMIT = 3 * 1024 * 1024       # bigger files go up in pieces
CHUNK = 4 * 1024 * 1024 - (4 * 1024 * 1024) % (320 * 1024)   # Graph wants multiples of 320 KiB


def configured(settings: Settings) -> bool:
    return bool(settings.microsoft_client_id and settings.microsoft_client_secret)


def _tenant(settings: Settings) -> str:
    return settings.microsoft_tenant or "common"


def auth_url(settings: Settings, redirect_uri: str, state: str) -> str:
    q = {"client_id": settings.microsoft_client_id, "redirect_uri": redirect_uri, "response_type": "code", "response_mode": "query",
         "scope": SCOPE, "state": state, "prompt": "select_account"}
    return f"{LOGIN}/{_tenant(settings)}/oauth2/v2.0/authorize?" + urlencode(q)


def _token(settings: Settings, data: dict) -> dict:
    body = {"client_id": settings.microsoft_client_id, "client_secret": settings.microsoft_client_secret, "scope": SCOPE, **data}
    r = httpx.post(f"{LOGIN}/{_tenant(settings)}/oauth2/v2.0/token", data=body, timeout=30)
    if r.status_code >= 400:
        raise MailError("Microsoft did not accept the sign-in; connect the mailbox again")
    return r.json()


def exchange_code(settings: Settings, code: str, redirect_uri: str) -> dict:
    """The one-time code from Microsoft's sign-in becomes a refresh token. Returns {refresh_token, access_token}."""
    return _token(settings, {"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})


class Outlook:
    """Thin client over Microsoft Graph for one connected mailbox. Same shape filing uses for Gmail and IMAP."""

    kind = "outlook"

    def __init__(self, settings: Settings, refresh_token: str, address: str = "", on_token: Callable[[str], None] | None = None):
        self.settings = settings
        self.refresh_token = refresh_token
        self.address = address
        self.on_token = on_token      # Microsoft hands out a new refresh token as it goes; the store keeps the newest
        self._access = ""

    # --- connecting ------------------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        if not self._access:
            out = _token(self.settings, {"refresh_token": self.refresh_token, "grant_type": "refresh_token"})
            self._access = out["access_token"]
            fresh = out.get("refresh_token", "")
            if fresh and fresh != self.refresh_token:
                self.refresh_token = fresh
                if self.on_token:
                    self.on_token(fresh)
        # immutable ids: a message keeps its id when the office moves it to another folder
        return {"Authorization": f"Bearer {self._access}", "Prefer": 'IdType="ImmutableId"'}

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = httpx.request(method, path if path.startswith("https://") else f"{API}/{path}", headers=self._headers(), timeout=60, **kw)
        if r.status_code == 401:
            raise MailError("Microsoft signed Closeout out of the mailbox; connect it again")
        if r.status_code == 403:
            raise MailError("the Microsoft account does not let Closeout read or send this mail; an administrator may need to allow it")
        r.raise_for_status()
        return r

    def close(self) -> None:
        return None

    def profile(self) -> str:
        me = self._call("GET", API, params={"$select": "mail,userPrincipalName"}).json()
        return me.get("mail") or me.get("userPrincipalName") or ""

    # --- sending ---------------------------------------------------------------------------------------------------
    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> dict:
        """Write it as a draft (so it has its ids), attach the files, then send. Replies share its conversation."""
        draft = self._call("POST", "messages", json={"subject": subject, "body": {"contentType": "Text", "content": body},
                                                     "toRecipients": [{"emailAddress": {"address": to}}]}).json()
        mid = draft["id"]
        for name, data, mime in attachments:
            if len(data) <= INLINE_LIMIT:
                self._call("POST", f"messages/{mid}/attachments", json={"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                                                        "contentType": mime, "contentBytes": base64.b64encode(data).decode()})
            else:
                self._upload(mid, name, data)
        self._call("POST", f"messages/{mid}/send")
        return {"id": _key(draft), "thread_id": draft.get("conversationId", "")}

    def _upload(self, mid: str, name: str, data: bytes) -> None:
        session = self._call("POST", f"messages/{mid}/attachments/createUploadSession",
                             json={"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(data)}}).json()
        url, total = session["uploadUrl"], len(data)
        for start in range(0, total, CHUNK):
            part = data[start:start + CHUNK]
            end = start + len(part) - 1
            r = httpx.put(url, content=part, timeout=120,        # the upload link carries its own permission
                          headers={"Content-Length": str(len(part)), "Content-Range": f"bytes {start}-{end}/{total}"})
            r.raise_for_status()

    # --- reading ---------------------------------------------------------------------------------------------------
    # Messages are named by their email Message-ID, not Graph's id: a message the office sends to its own address has one
    # copy in Sent and another in the Inbox under different Graph ids, and only the Message-ID says they are the same mail.
    def _inbox(self, **params) -> list[dict]:
        return self._call("GET", "mailFolders/inbox/messages", params={"$top": 50, **params}).json().get("value", [])

    def thread_message_ids(self, thread_id: str) -> list[str]:
        """Messages in the inbox that belong to the conversation Closeout started."""
        if not thread_id:
            return []
        found = self._inbox(**{"$filter": f"conversationId eq '{_quoted(thread_id)}'", "$select": "id,internetMessageId"})
        return [_key(m) for m in found]

    def subject_ids(self, ref: str) -> list[str]:
        """Inbox messages from the last 90 days whose subject carries a review's reference."""
        since = datetime.now(timezone.utc) - timedelta(days=90)
        found = self._inbox(**{"$search": f'"subject:{ref}"', "$select": "id,internetMessageId,subject,receivedDateTime"})
        out = []
        for m in found:
            got = (m.get("receivedDateTime") or "").replace("Z", "+00:00")
            try:
                recent = datetime.fromisoformat(got) >= since
            except ValueError:
                recent = True
            if ref in (m.get("subject") or "") and recent:
                out.append(_key(m))
        return out

    def _graph_id(self, message_id: str) -> tuple[str, str]:
        """Graph's id and conversation for a message named by its Message-ID, preferring the Inbox copy."""
        if message_id.startswith("<"):
            found = self._inbox(**{"$filter": f"internetMessageId eq '{_quoted(message_id)}'", "$select": "id,conversationId"})
            if found:
                return found[0]["id"], found[0].get("conversationId", "")
        meta = self._call("GET", f"messages/{quote(message_id, safe='')}", params={"$select": "id,conversationId"}).json()
        return meta["id"], meta.get("conversationId", "")

    def message(self, message_id: str) -> Incoming:
        gid, conversation = self._graph_id(message_id)
        raw = self._call("GET", f"messages/{quote(gid, safe='')}/$value").content
        inc = parse_raw(raw, self.address)
        inc.id, inc.thread_id, inc.refs = message_id, conversation, REFS.findall(" ".join(_headers_of(raw)))
        return inc

    def mark(self, message_id: str, add: str, remove: str = "") -> None:
        """Nothing is changed in the office's mailbox; the inbound record already stops a second read."""
        return None

    # --- the office's whole mailbox, for its Emails page -----------------------------------------------------------
    def list_messages(self, folder: str = "inbox", q: str = "", limit: int = 30) -> list[dict]:
        params = {"$top": limit, "$select": "id,conversationId,from,toRecipients,subject,bodyPreview,receivedDateTime,isRead"}
        if q:
            params["$search"] = '"' + q.replace('"', " ") + '"'
        else:
            params["$orderby"] = "receivedDateTime desc"
        path = "mailFolders/sentitems/messages" if folder == "sent" else "mailFolders/inbox/messages"
        out = []
        for m in self._call("GET", path, params=params).json().get("value", []):
            sender = (m.get("from") or {}).get("emailAddress") or {}
            frm = f'{sender.get("name", "")} <{sender.get("address", "")}>' if sender.get("address") else sender.get("name", "")
            to = ", ".join((r.get("emailAddress") or {}).get("address", "") for r in m.get("toRecipients") or [])
            out.append(summary(m["id"], m.get("conversationId", ""), frm, to, m.get("subject", ""), m.get("bodyPreview", ""),
                               m.get("receivedDateTime", ""), not m.get("isRead", True), self.address))
        return out

    def open_thread(self, message_id: str) -> list[dict]:
        meta = self._call("GET", f"messages/{quote(message_id, safe='')}", params={"$select": "id,conversationId"}).json()
        ids = [meta["id"]]
        if meta.get("conversationId"):
            found = self._call("GET", "messages", params={"$filter": f"conversationId eq '{_quoted(meta['conversationId'])}'",
                                                          "$select": "id,receivedDateTime", "$top": 50}).json().get("value", [])
            found.sort(key=lambda m: m.get("receivedDateTime") or "")
            ids = [m["id"] for m in found][-20:] or ids
        out = []
        for gid in ids:
            raw = self._call("GET", f"messages/{quote(gid, safe='')}/$value").content
            out.append({**view_raw(raw, self.address), "id": gid})
        return out

    def reply(self, message_id: str, body: str) -> dict:
        """Microsoft keeps the reply in the same conversation and in Sent Items."""
        self._call("POST", f"messages/{quote(message_id, safe='')}/reply", json={"comment": body})
        return {"id": "", "thread_id": ""}


def _key(m: dict) -> str:
    return m.get("internetMessageId") or m["id"]


def _quoted(value: str) -> str:
    return value.replace("'", "''")


def _headers_of(raw: bytes) -> list[str]:
    from email import policy
    from email.parser import BytesParser
    head = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return [str(head.get("In-Reply-To", "")), str(head.get("References", ""))]
