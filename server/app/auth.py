"""Microsoft Entra sign-in (OpenID Connect, authorization code + PKCE). Sessions live in memory only."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field

import httpx
import jwt

from .config import settings

COOKIE = "tm_sid"


@dataclass
class Session:
    id: str
    expires: float
    user: dict | None = None
    pending: dict = field(default_factory=dict)


_sessions: dict[str, Session] = {}


def get_session(sid: str | None) -> tuple[Session, bool]:
    now = time.time()
    for k in [k for k, s in _sessions.items() if s.expires < now]:
        _sessions.pop(k, None)
    s = _sessions.get(sid or "")
    if s:
        return s, False
    s = Session(id=secrets.token_urlsafe(32), expires=now + settings.session_hours * 3600)
    _sessions[s.id] = s
    return s, True


def drop_session(sid: str) -> None:
    _sessions.pop(sid, None)


def _authority() -> str:
    return f"https://login.microsoftonline.com/{settings.entra.tenant_id}"


def redirect_uri() -> str:
    return f"{settings.base_url}/auth/callback"


def login_url(s: Session) -> str:
    verifier = secrets.token_urlsafe(48)
    s.pending = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": verifier}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = httpx.QueryParams(
        client_id=settings.entra.client_id,
        response_type="code",
        redirect_uri=redirect_uri(),
        scope="openid profile email",
        state=s.pending["state"],
        nonce=s.pending["nonce"],
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return f"{_authority()}/oauth2/v2.0/authorize?{q}"


_jwks: jwt.PyJWKClient | None = None


def finish_login(s: Session, params: dict) -> dict:
    pending, s.pending = s.pending, {}
    if params.get("error"):
        raise ValueError(params.get("error_description") or params["error"])
    if not pending or params.get("state") != pending.get("state") or not params.get("code"):
        raise ValueError("Sign-in could not be verified. Start again from the sign-in page.")
    r = httpx.post(
        f"{_authority()}/oauth2/v2.0/token",
        data={
            "client_id": settings.entra.client_id,
            "client_secret": settings.entra.client_secret,
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": redirect_uri(),
            "code_verifier": pending["verifier"],
        },
        timeout=30,
    )
    j = r.json()
    if r.status_code >= 400:
        raise ValueError(j.get("error_description") or j.get("error") or f"Token request failed ({r.status_code})")
    global _jwks
    _jwks = _jwks or jwt.PyJWKClient(f"{_authority()}/discovery/v2.0/keys")
    key = _jwks.get_signing_key_from_jwt(j["id_token"]).key
    claims = jwt.decode(j["id_token"], key, algorithms=["RS256"], audience=settings.entra.client_id, issuer=f"{_authority()}/v2.0")
    if claims.get("nonce") != pending["nonce"]:
        raise ValueError("Sign-in could not be verified (nonce mismatch).")
    if settings.entra.allowed_group_id:
        groups = claims.get("groups") or []
        if settings.entra.allowed_group_id not in groups:
            raise ValueError("Your account is not in the group allowed to use this app.")
    return {
        "id": claims.get("oid", claims.get("sub")),
        "upn": claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        "name": claims.get("name") or claims.get("preferred_username") or "",
    }
