"""AUTH_MODE=public tests: real login (username/password + session cookie),
rate limiting, the passkey (WebAuthn) 2nd-factor enrollment/login round trip,
and findings write gating with no owner session at all (only an MCP secret).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..")
for p in (APP, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["AUTH_MODE"] = "public"
os.environ["COUCHDB_URL"] = "http://fake:5984"
os.environ["COUCHDB_USER"] = "admin"
os.environ["COUCHDB_PASSWORD"] = ""
os.environ["MCP_SECRET"] = "the-mcp-secret"
os.environ["SESSION_SECRET"] = "test-session-secret-not-for-prod"
os.environ["FEVER_THRESHOLD"] = "37.8"
os.environ["WEBAUTHN_RP_ID"] = "localhost"
os.environ["WEBAUTHN_RP_NAME"] = "Digestary Test"
os.environ["WEBAUTHN_ORIGIN"] = "http://localhost"

for m in list(sys.modules):
    if m in ("server", "couch", "auth"):
        del sys.modules[m]

from fastapi.testclient import TestClient  # noqa: E402
import couch as couch_module               # noqa: E402
import server                              # noqa: E402
from fake_couch import install_fake        # noqa: E402

client = TestClient(server.app)


def setup_function(_):
    install_fake(couch_module)
    client.cookies.clear()
    server._auth["limiter"]._attempts.clear()
    server._auth["create_user"]("owner1", "correct-horse-battery")


def test_owner_must_log_in_before_writing():
    r = client.post("/api/intake", json={"food_ids": ["eple"]})
    assert r.status_code == 401


def test_login_then_session_cookie_authorizes_writes():
    r = client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    assert r.status_code == 200
    assert r.json()["role"] == "owner"
    assert "digestary_session" in r.cookies

    r2 = client.post("/api/intake", json={"food_ids": ["eple"]})
    assert r2.status_code == 200

    me = client.get("/api/auth/me")
    assert me.json() == {"username": "owner1", "role": "owner"}


def test_wrong_password_is_rejected_and_rate_limited():
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "owner1", "password": "nope"})
        assert r.status_code == 401
    locked = client.post("/api/auth/login", json={"username": "owner1", "password": "nope"})
    assert locked.status_code == 429
    # even the correct password is now locked out for the rest of the window
    still_locked = client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    assert still_locked.status_code == 429


def test_no_token_at_all_is_rejected():
    r = client.post("/api/notes", json={"text": "hi"})
    assert r.status_code == 401


def test_owner_login_can_delete_and_manage_catalog():
    client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    r = client.post("/api/items", json={"name": "ny_mat"})
    assert r.status_code == 200
    r2 = client.delete("/api/items/ny_mat")
    assert r2.status_code == 200


def test_logout_clears_session():
    client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    assert client.get("/api/auth/me").json()["role"] == "owner"
    client.post("/api/auth/logout")
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_findings_over_http_needs_mcp_secret_when_no_owner_session():
    r = client.post("/api/findings", json={"title": "x", "findings": []},
                     headers={"X-MCP-Secret": "wrong"})
    assert r.status_code == 401
    r2 = client.post("/api/findings", json={"title": "x", "findings": []},
                      headers={"X-MCP-Secret": "the-mcp-secret"})
    assert r2.status_code == 200
    assert r2.json()["author"] == "mcp"


def test_creating_a_second_owner_account_requires_an_existing_owner():
    r = client.post("/api/auth/users", json={"username": "partner", "password": "another-strong-pw"})
    assert r.status_code == 401  # not logged in
    client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    r2 = client.post("/api/auth/users", json={"username": "partner", "password": "another-strong-pw"})
    assert r2.status_code == 200
    assert "password_hash" not in r2.json()


def test_passkey_register_then_becomes_required_second_factor_on_login():
    """Full round trip against the real webauthn verification path (via a
    software authenticator — see soft_authenticator.py), matching how the
    frontend's navigator.credentials.create()/get() calls are wired in
    app.js: begin -> browser ceremony -> complete, then login -> mfa_required
    -> verify."""
    from soft_authenticator import SoftAuthenticator

    client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    assert client.get("/api/config").json()["passkeys_enabled"] is True

    options = client.post("/api/auth/passkeys/register/begin").json()
    authenticator = SoftAuthenticator()
    credential = authenticator.create(options["challenge"], "localhost", "http://localhost")
    reg = client.post("/api/auth/passkeys/register/complete",
                       json={"nickname": "Test Key", "credential": credential})
    assert reg.status_code == 200
    assert reg.json()["nickname"] == "Test Key"
    assert "public_key" not in reg.json()

    # password alone must no longer be enough for this account
    client.post("/api/auth/logout")
    client.cookies.clear()
    login = client.post("/api/auth/login", json={"username": "owner1", "password": "correct-horse-battery"})
    assert login.status_code == 200
    body = login.json()
    assert body["mfa_required"] is True
    assert "digestary_session" not in login.cookies

    assertion = authenticator.get(body["options"]["challenge"], "localhost", "http://localhost")
    verify = client.post("/api/auth/passkeys/login/verify",
                          json={"ticket": body["ticket"], "credential": assertion})
    assert verify.status_code == 200
    assert verify.json() == {"username": "owner1", "role": "owner"}
    assert "digestary_session" in verify.cookies

    # a reused ticket must not work twice (single-use)
    replay = client.post("/api/auth/passkeys/login/verify",
                          json={"ticket": body["ticket"], "credential": assertion})
    assert replay.status_code == 400

    passkeys = client.get("/api/auth/passkeys").json()
    assert len(passkeys) == 1
    assert client.delete(f"/api/auth/passkeys/{passkeys[0]['_id']}").status_code == 200
    assert client.get("/api/auth/passkeys").json() == []


def test_passkeys_disabled_without_webauthn_env_config():
    import auth as auth_module

    for k in ("WEBAUTHN_RP_ID", "WEBAUTHN_ORIGIN"):
        os.environ.pop(k, None)
    try:
        cfg = auth_module.AuthConfig()
        assert cfg.passkeys_enabled is False
    finally:
        os.environ["WEBAUTHN_RP_ID"] = "localhost"
        os.environ["WEBAUTHN_ORIGIN"] = "http://localhost"
