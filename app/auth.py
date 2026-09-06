"""Authentication: real username/password accounts (with sessions) plus a
lightweight guest code, gated by AUTH_MODE.

Design note (why this shape): this app started with only a shared
OWNER_TOKEN/GUEST_TOKEN header, which is fine for a solo LAN device but not
for "a health journal someone might expose more broadly" or for a second
household member to have their own login. Real accounts are implemented now,
not stubbed, specifically so this layer never needs a rewrite to grow a real
auth system later — a passkey (WebAuthn) or TOTP second factor can be added
on top of the session mechanism below without touching how passwords or
sessions work.

- AUTH_MODE=local (default): no login at all; every request is the sole
  owner. Good for a private home device.
- AUTH_MODE=public: owner actions require a logged-in session (username +
  password -> signed cookie). A GUEST_TOKEN (if set) still allows add-only
  access with no account, for a one-off shared device (see README).

Passwords: PBKDF2-HMAC-SHA256, per-user random salt, no third-party crypto
dependency. Sessions: a small HMAC-signed cookie (no server-side session
store needed for a single-process app) carrying {username, role,
session_version, exp}; bumping a user's session_version invalidates every
outstanding session for that user (used by logout-everywhere / password
change). Login attempts are rate-limited in-memory (per-process; resets on
restart, which is an acceptable trade-off for a LAN-scale personal tool).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, Header, HTTPException

from couch import Couch, now_iso

PBKDF2_ITERATIONS = 210_000
SESSION_COOKIE = "digestary_session"


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


@dataclass
class Principal:
    username: str
    role: str  # "owner" | "guest"


class AuthConfig:
    def __init__(self):
        self.mode = os.environ.get("AUTH_MODE", "local").lower()  # local | public
        self.guest_token = os.environ.get("GUEST_TOKEN", "")
        self.session_secret = os.environ.get("SESSION_SECRET", "")
        self.session_days = int(os.environ.get("SESSION_DAYS", "30"))
        self.cookie_secure = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
        if self.mode == "public" and not self.session_secret:
            raise RuntimeError(
                "AUTH_MODE=public requires SESSION_SECRET to be set (see .env.example — "
                "generate one with `openssl rand -hex 32`)."
            )


def make_auth(couch: Couch, cfg: AuthConfig):
    """Build the FastAPI dependencies bound to this app's Couch client + config."""
    limiter = LoginRateLimiter()

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

    def login(username: str, password: str) -> str:
        uid = username.lower().strip()
        limiter.check(uid)
        user = get_user(uid)
        if not user or not verify_password(password, user["salt"], user["password_hash"]):
            limiter.record_failure(uid)
            raise HTTPException(status_code=401, detail="invalid username or password")
        limiter.clear(uid)
        return sign_session(cfg.session_secret, uid, user["role"],
                             user.get("session_version", 1),
                             cfg.session_days * 86400)

    def logout_everywhere(username: str) -> None:
        uid = username.lower().strip()
        user = get_user(uid)
        if user:
            user["session_version"] = user.get("session_version", 1) + 1
            couch.put("users", uid, user)

    def try_resolve_principal(x_auth_token: Optional[str],
                               session_cookie: Optional[str]) -> Optional[Principal]:
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
        if cfg.guest_token and x_auth_token and hmac.compare_digest(x_auth_token, cfg.guest_token):
            return Principal(username="guest", role="guest")
        return None

    def resolve_principal(
        x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token"),
        session_cookie: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> Principal:
        p = try_resolve_principal(x_auth_token, session_cookie)
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
        "logout_everywhere": logout_everywhere,
        "resolve_principal": resolve_principal,
        "try_resolve_principal": try_resolve_principal,
        "require_owner": require_owner,
        "limiter": limiter,
    }
