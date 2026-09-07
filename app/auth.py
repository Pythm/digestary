"""Authentication: real username/password accounts (with sessions), gated by
AUTH_MODE, plus an optional per-account WebAuthn/passkey second factor.

Design note (why this shape): this app started with only a shared
OWNER_TOKEN header, which is fine for a solo LAN device but not for "a
health journal someone might expose more broadly" or for a second household
member to have their own login. Real accounts are implemented now, not
stubbed, specifically so this layer never needs a rewrite to grow a real
auth system later — a passkey (WebAuthn) second factor is exactly that
addition, layered on top of the session mechanism below without touching
how passwords or sessions work.

- AUTH_MODE=local (default): no login at all; every request is the sole
  owner. Good for a private home device.
- AUTH_MODE=public: owner actions require a logged-in session (username +
  password -> signed cookie). If that account has enrolled a passkey (see
  the Security section in the UI), login is two-step: a correct password
  returns a one-time ticket + WebAuthn challenge instead of a session, and
  the session cookie is only issued once /api/auth/passkeys/login/verify
  confirms the passkey assertion against that ticket.

Passwords: PBKDF2-HMAC-SHA256, per-user random salt, no third-party crypto
dependency. Sessions: a small HMAC-signed cookie (no server-side session
store needed for a single-process app) carrying {username, role,
session_version, exp}; bumping a user's session_version invalidates every
outstanding session for that user (used by logout-everywhere / password
change). Login attempts are rate-limited in-memory (per-process; resets on
restart, which is an acceptable trade-off for a LAN-scale personal tool).

Passkeys need WEBAUTHN_RP_ID + WEBAUTHN_ORIGIN set (see .env.example) —
unset means the feature is simply hidden/disabled everywhere, the same
"leave it blank to not use it" shape as every other optional feature in
this app. WebAuthn is a browser API that refuses to run outside a secure
context: WEBAUTHN_ORIGIN must be an `https://` origin (or exactly
`http://localhost:<port>` for local dev) — it will not work over a bare
LAN IP/HTTP, which is this app's documented default deployment, so a
reverse proxy terminating TLS is required to actually use this in
production (see README). Registration/login challenges are held in the
same kind of in-memory, per-process, short-TTL store as the rate limiter
above — never persisted, never needed across a restart.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, HTTPException
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

from couch import Couch, now_iso

PBKDF2_ITERATIONS = 210_000
SESSION_COOKIE = "digestary_session"
CHALLENGE_TTL_SECONDS = 300


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    digest, _ = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(digest, hash_hex)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_session(secret: str, username: str, role: str, session_version: int,
                  ttl_seconds: int) -> str:
    payload = {"u": username, "r": role, "v": session_version,
               "exp": int(time.time()) + ttl_seconds}
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(secret: str, token: Optional[str]) -> Optional[dict]:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


class LoginRateLimiter:
    """In-memory sliding-window limiter: N failed attempts per window per
    username locks that username out for the rest of the window."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        hist = [t for t in self._attempts.get(key, []) if now - t < self.window_seconds]
        self._attempts[key] = hist
        if len(hist) >= self.max_attempts:
            raise HTTPException(status_code=429,
                                 detail="Too many login attempts — try again in a few minutes")

    def record_failure(self, key: str) -> None:
        self._attempts.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)


class TicketStore:
    """In-memory, short-TTL, single-use store for WebAuthn challenges —
    one bucket for registration (keyed by username: a user can only be
    mid-registration once) and one for login (keyed by a random ticket,
    since a fresh ticket is minted per login attempt)."""

    def __init__(self, ttl_seconds: int = CHALLENGE_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}

    def put(self, key: str, data: dict) -> None:
        self._store[key] = (time.time() + self.ttl_seconds, data)

    def pop(self, key: str) -> Optional[dict]:
        entry = self._store.pop(key, None)
        if not entry:
            return None
        expires_at, data = entry
        if expires_at < time.time():
            return None
        return data


@dataclass
class Principal:
    username: str
    role: str  # "owner"


class AuthConfig:
    def __init__(self):
        self.mode = os.environ.get("AUTH_MODE", "local").lower()  # local | public
        self.session_secret = os.environ.get("SESSION_SECRET", "")
        self.session_days = int(os.environ.get("SESSION_DAYS", "30"))
        self.cookie_secure = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
        if self.mode == "public" and not self.session_secret:
            raise RuntimeError(
                "AUTH_MODE=public requires SESSION_SECRET to be set (see .env.example — "
                "generate one with `openssl rand -hex 32`)."
            )
        # passkeys: optional, disabled unless both are set (see module docstring)
        self.webauthn_rp_id = os.environ.get("WEBAUTHN_RP_ID", "").strip()
        self.webauthn_rp_name = os.environ.get("WEBAUTHN_RP_NAME", "Digestary").strip()
        self.webauthn_origin = os.environ.get("WEBAUTHN_ORIGIN", "").strip().rstrip("/")

    @property
    def passkeys_enabled(self) -> bool:
        return bool(self.webauthn_rp_id and self.webauthn_origin)


def make_auth(couch: Couch, cfg: AuthConfig):
    """Build the FastAPI dependencies bound to this app's Couch client + config."""
    limiter = LoginRateLimiter()
    registration_challenges = TicketStore()   # keyed by username
    login_tickets = TicketStore()             # keyed by a random per-attempt ticket

    def get_user(username: str) -> Optional[dict]:
        return couch.get("users", username.lower().strip())

    def create_user(username: str, password: str, role: str = "owner") -> dict:
        uid = username.lower().strip()
        if not uid or not password or len(password) < 8:
            raise HTTPException(status_code=400,
                                 detail="username required; password must be at least 8 characters")
        if get_user(uid):
            raise HTTPException(status_code=409, detail="that username already exists")
        pw_hash, salt = hash_password(password)
        doc = {
            "username": uid,
            "password_hash": pw_hash,
            "salt": salt,
            "role": role,
            "session_version": 1,
            "created_at": now_iso(),
        }
        return couch.put("users", uid, doc)

    def issue_session(user: dict) -> dict:
        token = sign_session(cfg.session_secret, user["username"], user["role"],
                              user.get("session_version", 1), cfg.session_days * 86400)
        return {"session_token": token, "username": user["username"], "role": user["role"]}

    def list_passkeys(username: str) -> list[dict]:
        return couch.mango("passkeys", {"username": username}, limit=50)

    def login(username: str, password: str) -> dict:
        """Password step. Returns either an issued session (no passkeys
        enrolled) or an {"mfa_required": True, ...} challenge to complete
        via complete_passkey_login below — see module docstring."""
        uid = username.lower().strip()
        limiter.check(uid)
        user = get_user(uid)
        if not user or not verify_password(password, user["salt"], user["password_hash"]):
            limiter.record_failure(uid)
            raise HTTPException(status_code=401, detail="invalid username or password")
        limiter.clear(uid)
        creds = list_passkeys(uid)
        if not creds:
            return issue_session(user)
        if not cfg.passkeys_enabled:
            raise HTTPException(
                status_code=503,
                detail="this account has a passkey enrolled but the server's "
                       "WEBAUTHN_RP_ID/WEBAUTHN_ORIGIN are not configured — fix the "
                       "server config rather than silently skipping the 2nd factor",
            )
        ticket = uuid.uuid4().hex
        options = generate_authentication_options(
            rp_id=cfg.webauthn_rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=_b64u_decode(c["credential_id"])) for c in creds
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        login_tickets.put(ticket, {"username": uid, "challenge": options.challenge})
        return {"mfa_required": True, "ticket": ticket, "options": json.loads(options_to_json(options))}

    def complete_passkey_login(ticket: str, credential: dict) -> dict:
        pending = login_tickets.pop(ticket)
        if not pending:
            raise HTTPException(status_code=400, detail="login challenge expired or invalid — sign in again")
        uid = pending["username"]
        credential_id = credential.get("id") or credential.get("rawId") or ""
        passkey = couch.get("passkeys", f"pk-{credential_id}")
        if not passkey or passkey.get("username") != uid:
            raise HTTPException(status_code=401, detail="unrecognized passkey")
        try:
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=pending["challenge"],
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin,
                credential_public_key=_b64u_decode(passkey["public_key"]),
                credential_current_sign_count=passkey["sign_count"],
            )
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"passkey verification failed: {e}")
        passkey["sign_count"] = verification.new_sign_count
        couch.put("passkeys", passkey["_id"], passkey)
        user = get_user(uid)
        return issue_session(user)

    def begin_passkey_registration(username: str) -> dict:
        if not cfg.passkeys_enabled:
            raise HTTPException(
                status_code=400,
                detail="passkeys are not configured on this server (set WEBAUTHN_RP_ID "
                       "and WEBAUTHN_ORIGIN — see .env.example / README)",
            )
        existing = list_passkeys(username)
        options = generate_registration_options(
            rp_id=cfg.webauthn_rp_id,
            rp_name=cfg.webauthn_rp_name,
            user_id=username.encode(),
            user_name=username,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=_b64u_decode(c["credential_id"])) for c in existing
            ],
        )
        registration_challenges.put(username, {"challenge": options.challenge})
        return json.loads(options_to_json(options))

    def complete_passkey_registration(username: str, nickname: str, credential: dict) -> dict:
        pending = registration_challenges.pop(username)
        if not pending:
            raise HTTPException(status_code=400, detail="registration challenge expired or invalid — try again")
        try:
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=pending["challenge"],
                expected_rp_id=cfg.webauthn_rp_id,
                expected_origin=cfg.webauthn_origin,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"passkey registration failed: {e}")
        credential_id = _b64u(verification.credential_id)
        doc = {
            "username": username,
            "credential_id": credential_id,
            "public_key": _b64u(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "nickname": (nickname or "Passkey").strip()[:60],
            "created_at": now_iso(),
        }
        return couch.put("passkeys", f"pk-{credential_id}", doc)

    def delete_passkey(username: str, doc_id: str) -> None:
        doc = couch.get("passkeys", doc_id)
        if not doc or doc.get("username") != username:
            raise HTTPException(status_code=404, detail="passkey not found")
        couch.delete("passkeys", doc_id)

    def logout_everywhere(username: str) -> None:
        uid = username.lower().strip()
        user = get_user(uid)
        if user:
            user["session_version"] = user.get("session_version", 1) + 1
            couch.put("users", uid, user)

    def try_resolve_principal(session_cookie: Optional[str]) -> Optional[Principal]:
        """Same resolution as resolve_principal, but returns None instead of
        raising — for the few endpoints (e.g. POST /api/findings) that must
        also accept a plain MCP-secret caller with no session at all."""
        if cfg.mode != "public":
            return Principal(username="owner", role="owner")
        if session_cookie:
            payload = read_session(cfg.session_secret, session_cookie)
            if payload:
                user = get_user(payload["u"])
                if user and user.get("session_version", 1) == payload.get("v"):
                    return Principal(username=payload["u"], role=payload["r"])
        return None

    def resolve_principal(
        session_cookie: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> Principal:
        p = try_resolve_principal(session_cookie)
        if p:
            return p
        raise HTTPException(status_code=401, detail="login required (see POST /api/auth/login)")

    def require_owner(p: Principal) -> Principal:
        if p.role != "owner":
            raise HTTPException(status_code=403, detail="owner access required for this operation")
        return p

    return {
        "get_user": get_user,
        "create_user": create_user,
        "login": login,
        "complete_passkey_login": complete_passkey_login,
        "list_passkeys": list_passkeys,
        "begin_passkey_registration": begin_passkey_registration,
        "complete_passkey_registration": complete_passkey_registration,
        "delete_passkey": delete_passkey,
        "logout_everywhere": logout_everywhere,
        "resolve_principal": resolve_principal,
        "try_resolve_principal": try_resolve_principal,
        "require_owner": require_owner,
        "limiter": limiter,
    }
