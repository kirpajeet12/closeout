"""The office's own work email, connected with its address and an app password: any provider that speaks IMAP (reading)
and SMTP (sending), so Gmail, Outlook, Yahoo, iCloud, Zoho, Hostinger, GoDaddy and most company mail. Closeout reads
only replies to messages it sent and mail whose subject carries a review's reference; it reads without marking anything
as read. The password lives in the local database, never in source, logs or a page."""

from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import make_msgid

from .gmail import Incoming, build_message, parse_raw

# provider presets by the part after @: (imap host, imap port, smtp host, smtp port)
PRESETS: dict[str, tuple[str, int, str, int]] = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "live.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "msn.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "yahoo.ca": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "mac.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "zoho.com": ("imap.zoho.com", 993, "smtp.zoho.com", 465),
    "fastmail.com": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "aol.com": ("imap.aol.com", 993, "smtp.aol.com", 465),
}
# who hosts a company domain's mail, for when the address alone does not say: the office picks one
HOSTS: dict[str, tuple[str, int, str, int]] = {
    "google": PRESETS["gmail.com"],
    "microsoft": PRESETS["outlook.com"],
    "hostinger": ("imap.hostinger.com", 993, "smtp.hostinger.com", 465),
    "godaddy": ("imap.secureserver.net", 993, "smtpout.secureserver.net", 465),
    "zoho": PRESETS["zoho.com"],
}
REFS = re.compile(r"<[^<>\s]+>")


def servers_for(address: str, host: str = "") -> tuple[str, int, str, int]:
    """Where a mailbox most likely lives: the host the office picked, a known provider, else imap.<domain> and smtp.<domain>."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    return HOSTS.get(host) or PRESETS.get(domain) or (f"imap.{domain}", 993, f"smtp.{domain}", 465)


def _ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class MailError(Exception):
    """A plain sentence the office can act on."""


class ImapMail:
    """One work mailbox. Same shape filing uses for the Gmail connection: send, replies, message, mark."""

    kind = "email"

    def __init__(self, address: str, password: str, imap_host: str = "", imap_port: int = 0, smtp_host: str = "", smtp_port: int = 0,
                 username: str = "", host: str = ""):
        ih, ip, sh, sp = servers_for(address, host)
        self.address = address
        self.username = username or address
        self.password = password
        self.imap_host, self.imap_port = imap_host or ih, int(imap_port or ip)
        self.smtp_host, self.smtp_port = smtp_host or sh, int(smtp_port or sp)
        self._imap: imaplib.IMAP4 | None = None
        self._validity = ""

    # --- connecting ------------------------------------------------------------------------------------------------
    def _open(self) -> imaplib.IMAP4:
        if self._imap is None:
            try:
                box = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=_ctx(), timeout=30)
            except (OSError, imaplib.IMAP4.error) as e:
                raise MailError(f"could not reach {self.imap_host}; check the incoming server name") from e
            try:
                box.login(self.username, self.password)
            except imaplib.IMAP4.error as e:
                raise MailError("the email provider refused the address and password; use an app password if the account has "
                                "two-step sign-in, and make sure IMAP is turned on") from e
            typ, data = box.select("INBOX", readonly=True)
            if typ != "OK":
                raise MailError("the inbox could not be opened")
            v = box.response("UIDVALIDITY")[1]
            self._validity = (v[0].decode() if v and isinstance(v[0], bytes) else str(v[0] if v else "")) or "0"
            self._imap = box
        return self._imap

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def _smtp(self) -> smtplib.SMTP:
        try:
            if self.smtp_port == 465:
                s = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=_ctx(), timeout=30)
            else:
                s = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                s.starttls(context=_ctx())
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(f"could not reach {self.smtp_host}; check the outgoing server name") from e
        try:
            s.login(self.username, self.password)
        except smtplib.SMTPException as e:
            s.close()
            raise MailError("the email provider refused sending with that address and password") from e
        return s

    def verify(self) -> None:
        """Log in to both sides once, so a wrong password shows up when the office connects, not on the first send."""
        self._open()
        self.close()
        s = self._smtp()
        s.quit()

    def profile(self) -> str:
        return self.address

    # --- sending ---------------------------------------------------------------------------------------------------
    def send(self, to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> dict:
        """Send from the work address. Its Message-ID is the thread: replies name it in In-Reply-To or References."""
        msg = build_message(self.address, to, subject, body, attachments)
        mid = make_msgid(domain=self.address.rsplit("@", 1)[-1])
        msg["Message-ID"] = mid
        s = self._smtp()
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return {"id": mid, "thread_id": mid}

    # --- reading ---------------------------------------------------------------------------------------------------
    def _uids(self, *criteria: str) -> list[str]:
        box = self._open()
        typ, data = box.uid("SEARCH", *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return [f"{self._validity}:{u.decode()}" for u in data[0].split()]

    def _since(self, days: int = 90) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

    def thread_message_ids(self, thread_id: str) -> list[str]:
        """Messages in the inbox that answer the one Closeout sent."""
        ids: list[str] = []
        for header in ("In-Reply-To", "References"):
            for u in self._uids("SINCE", self._since(), "HEADER", header, f'"{thread_id}"'):
                if u not in ids:
                    ids.append(u)
        return ids

    def subject_ids(self, ref: str) -> list[str]:
        """Messages whose subject carries a review's reference: a new email the contractor wrote, or one sent from a mail app."""
        return self._uids("SINCE", self._since(), "SUBJECT", f'"{ref}"')

    def message(self, message_id: str) -> Incoming:
        box = self._open()
        validity, _, uid = message_id.partition(":")
        if validity != self._validity:
            raise MailError("the mailbox was rebuilt by the provider; look again")
        typ, data = box.uid("FETCH", uid, "(BODY.PEEK[])")    # PEEK: the office's unread mail stays unread
        raw = next((part[1] for part in data or [] if isinstance(part, tuple)), b"")
        inc = parse_raw(raw, self.address)
        head = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        refs = REFS.findall(f"{head.get('In-Reply-To', '')} {head.get('References', '')}")
        inc.id, inc.thread_id, inc.refs = message_id, (refs[0] if refs else ""), refs
        return inc

    def mark(self, message_id: str, add: str, remove: str = "") -> None:
        """Nothing to label in plain IMAP; the inbound record already stops a second read."""
        return None
