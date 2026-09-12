"""Sending the covering message to the contractor by email. The engineer presses Send with the address typed in;
nothing goes out on its own. With a verified sender on Amazon SES the app sends; without one the mail app does."""

from __future__ import annotations

import re

from .config import Settings

ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_address(text: str) -> bool:
    return bool(ADDRESS.match((text or "").strip()))


def can_send(settings: Settings) -> bool:
    return bool(settings.mail_from)


def send_email(settings: Settings, to: str, subject: str, body: str) -> str:
    """Send one plain-text message through Amazon SES and return its message id."""
    import boto3

    ses = boto3.client("sesv2", region_name=settings.region)
    out = ses.send_email(
        FromEmailAddress=settings.mail_from,
        Destination={"ToAddresses": [to]},
        ReplyToAddresses=[settings.mail_from],
        Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}},
    )
    return out.get("MessageId", "")
