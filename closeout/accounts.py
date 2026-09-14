"""Office accounts: passwords, one-time links (welcome and reset) and sign-in sessions.

Passwords are hashed with scrypt from the standard library. Links and session cookies are random tokens; only their
SHA-256 hash is stored, so a copy of the database cannot be used to sign in or to reset anyone's password."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

N, R, P = 2 ** 14, 8, 1
MIN_PASSWORD = 10
WELCOME_DAYS = 7
RESET_MINUTES = 60
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def password_problem(password: str, confirm: str) -> str:
    """What is wrong with a new password, or '' when it will do."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 200:
        return "Use 200 characters or fewer."
    if password != confirm:
        return "The two passwords do not match."
    return ""


def new_token() -> tuple[str, str]:
    """(token for the link or cookie, hash to store)."""
    t = secrets.token_urlsafe(32)
    return t, token_hash(t)


def token_hash(token: str) -> str:
    return hashlib.sha256(("closeout:" + (token or "")).encode()).hexdigest()


def later(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat(timespec="seconds")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")
