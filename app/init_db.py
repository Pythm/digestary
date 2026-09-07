#!/usr/bin/env python3
"""First-boot setup: create the 11 CouchDB databases + a Mango index on each
event-time field, and (in AUTH_MODE=public) bootstrap the first owner account
from OWNER_USERNAME/OWNER_PASSWORD if one doesn't exist yet.

This seeds NO catalog data. `items`, `symptom_items`, and `bathroom_items`
start empty and grow only from what the user types (or, optionally, from an
LLM connected over MCP — see README). Safe to re-run: every step here is
idempotent and never overwrites existing documents.
"""
from __future__ import annotations
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from couch import Couch  # noqa: E402
from auth import hash_password  # noqa: E402

COUCHDB_URL = os.environ.get("COUCHDB_URL", "http://127.0.0.1:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "admin")
COUCHDB_PASSWORD = os.environ.get("COUCHDB_PASSWORD", "")

DBS = ["items", "item_links", "intake", "holidays", "symptom_items", "health",
       "bathroom_items", "bathroom_events", "notes", "findings", "users", "passkeys"]

# db -> event-time field to index (only tables with a range-queried time)
EVENT_TIME_FIELDS = {
    "intake": "consumed_at",
    "health": "event_at",
    "bathroom_events": "event_at",
    "notes": "event_at",
}


def wait_for_couchdb(couch: Couch, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if couch.ping():
            return True
        time.sleep(2)
    return False


def bootstrap_owner(couch: Couch) -> None:
    if os.environ.get("AUTH_MODE", "local").lower() != "public":
        return
    username = os.environ.get("OWNER_USERNAME", "").strip().lower()
    password = os.environ.get("OWNER_PASSWORD", "")
    if not username or not password:
        print("AUTH_MODE=public but OWNER_USERNAME/OWNER_PASSWORD not set — "
              "no account will exist until one is created via POST /api/auth/users "
              "from an already-logged-in owner, which is impossible on first boot. "
              "Set both in .env before first run.")
        return
    if couch.get("users", username):
        return  # already bootstrapped
    pw_hash, salt = hash_password(password)
    couch.put("users", username, {
        "username": username, "password_hash": pw_hash, "salt": salt,
        "role": "owner", "session_version": 1,
    })
    print(f"Bootstrapped owner account {username!r}.")


def main() -> int:
    couch = Couch(COUCHDB_URL, COUCHDB_USER, COUCHDB_PASSWORD)
    if not wait_for_couchdb(couch):
        raise SystemExit("ERROR: CouchDB did not become ready in time. "
                          "Check COUCHDB_URL and the network.")

    for db in DBS:
        couch.create_db(db)
    print(f"Ensured {len(DBS)} databases.")

    for db, field in EVENT_TIME_FIELDS.items():
        couch.create_index(db, [field], name=f"{field}-index")
    couch.create_index("passkeys", ["username"], name="username-index")
    print("Ensured event-time indexes.")

    bootstrap_owner(couch)
    print("Done. No catalog data was seeded — items/symptom_items/bathroom_items start empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
