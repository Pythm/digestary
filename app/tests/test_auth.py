"""AUTH_MODE=public tests: real login (username/password + session cookie),
the guest token (add-only, no account), rate limiting, and findings write
gating with no owner session at all (only an MCP secret).
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
os.environ["GUEST_TOKEN"] = "guest456"
os.environ["SESSION_SECRET"] = "test-session-secret-not-for-prod"
os.environ["FEVER_THRESHOLD"] = "37.8"

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


def test_guest_token_can_add_but_not_delete_or_manage_catalog():
    r = client.post("/api/intake", json={"food_ids": ["eple"]}, headers={"X-Auth-Token": "guest456"})
    assert r.status_code == 200
    intake_id = r.json()["intake_id"]

    r2 = client.delete(f"/api/intake/group/{intake_id}", headers={"X-Auth-Token": "guest456"})
    assert r2.status_code == 403

    r3 = client.post("/api/items", json={"name": "ny_mat"}, headers={"X-Auth-Token": "guest456"})
    assert r3.status_code == 403


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
