"""Signing in with Google or Microsoft instead of a password.

Only the person's email address is asked for, never their mailbox. The address has to be one the office already added
under People: signing in with Google or Microsoft does not create an account, so a stranger with a Gmail address cannot
get in. The code is exchanged server to server with the client secret, over TLS, so the ID token that comes back is read
as issued (OpenID Connect Core 3.1.3.7)."""
from __future__ import annotations

import base64
import json
from urllib.parse import urlencode

import httpx

from . import gmail as gmail_mod
from . import outlook as outlook_mod
from .config import Settings

PROVIDERS = ("google", "microsoft")
GOOGLE_SCOPE = "openid email"
MICROSOFT_SCOPE = "openid email profile"


class SignInError(Exception):
    """A sentence the person can act on."""


def configured(provider: str, settings: Settings) -> bool:
    if provider == "google":
        return gmail_mod.configured(settings)
    if provider == "microsoft":
        return outlook_mod.configured(settings)
    return False


def auth_url(provider: str, settings: Settings, redirect_uri: str, state: str, nonce: str) -> str:
    if provider == "google":
        q = {"client_id": settings.google_client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": GOOGLE_SCOPE,
             "state": state, "nonce": nonce, "prompt": "select_account"}
        return gmail_mod.AUTH + "?" + urlencode(q)
    q = {"client_id": settings.microsoft_client_id, "redirect_uri": redirect_uri, "response_type": "code", "response_mode": "query",
         "scope": MICROSOFT_SCOPE, "state": state, "nonce": nonce, "prompt": "select_account"}
    return f"{outlook_mod.LOGIN}/{outlook_mod._tenant(settings)}/oauth2/v2.0/authorize?" + urlencode(q)


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise SignInError("the sign-in did not come back complete; try again")


def email_from(provider: str, settings: Settings, code: str, redirect_uri: str, nonce: str) -> str:
    """The verified email address of the account the person chose, lower case."""
    if provider == "google":
        url = gmail_mod.TOKEN
        data = {"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
        audience = settings.google_client_id
    else:
        url = f"{outlook_mod.LOGIN}/{outlook_mod._tenant(settings)}/oauth2/v2.0/token"
        data = {"code": code, "client_id": settings.microsoft_client_id, "client_secret": settings.microsoft_client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code", "scope": MICROSOFT_SCOPE}
        audience = settings.microsoft_client_id
    r = httpx.post(url, data=data, timeout=30)
    if r.status_code >= 400:
        raise SignInError("the sign-in was not accepted; try again")
    claims = _claims(r.json().get("id_token", ""))
    if claims.get("aud") != audience or claims.get("nonce") != nonce:
        raise SignInError("the sign-in did not match this page; try again")
    email = str(claims.get("email", "")).strip().lower()
    if provider == "google":
        verified = claims.get("email_verified") in (True, "true")
    else:
        # Microsoft leaves out the email claim when the address's domain is not verified; a work account whose
        # domain owner is unconfirmed (xms_edov false) is not trusted either
        verified = bool(email) and claims.get("xms_edov") is not False
    if not email or not verified:
        raise SignInError("that account has no confirmed email address; sign in with your password instead")
    return email
