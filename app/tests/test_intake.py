"""Single-timestamp + item/item-link/intake model tests. Replaces the old
test_two_timestamps.py (the two-timestamp idea — recorded_at + event_at — is
retired app-wide; see server.py's module docstring and deploy-plan.md §4.4).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..")
for p in (APP, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["AUTH_MODE"] = "local"
os.environ["COUCHDB_URL"] = "http://fake:5984"
os.environ["COUCHDB_USER"] = "admin"
os.environ["COUCHDB_PASSWORD"] = ""
os.environ["MCP_SECRET"] = "test-mcp-secret"
os.environ["FEVER_THRESHOLD"] = "37.8"
os.environ.pop("SESSION_SECRET", None)
os.environ.pop("GUEST_TOKEN", None)

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


def test_consumed_at_defaults_to_now_and_is_editable_to_past_or_future():
    r_no_time = client.post("/api/intake", json={"food_ids": ["eple"]})
    assert r_no_time.json()["consumed_at"] is not None  # defaulted to now

    past = "2020-01-01T08:00:00Z"
    r_past = client.post("/api/intake", json={"food_ids": ["eple"], "consumed_at": past})
    assert r_past.json()["consumed_at"] == past

    future = "2099-01-01T08:00:00Z"
    r_future = client.post("/api/intake", json={"food_ids": ["eple"], "consumed_at": future})
    assert r_future.json()["consumed_at"] == future


def test_a_leaf_item_and_a_parent_item_are_both_directly_selectable():
    client.post("/api/items", json={"name": "sylte"})
    client.post("/api/items", json={"name": "brød"})
    client.post("/api/item-links", json={"child": "sylte", "parent": "brød"})

    # sylte on its own, no bread involved — must work per §2.2.2
    r = client.post("/api/intake", json={"food_ids": ["sylte"]})
    assert r.status_code == 200
    assert r.json()["lines"][0]["food_id"] == "sylte"

    # bread plain (no sub-option lines) also works
    r2 = client.post("/api/intake", json={"food_ids": ["brød"]})
    assert len(r2.json()["lines"]) == 1


def test_a_sub_item_never_double_logs_inside_its_parent():
    # bread + butter + jam = three lines, not bread's own sub_items array
    r = client.post("/api/intake", json={"food_ids": ["brød", "smør", "sylte"],
                                          "where": "home_prepared"})
    food_ids = sorted(l["food_id"] for l in r.json()["lines"])
    assert food_ids == ["brød", "smør", "sylte"]


def test_where_out_prepared_carries_an_optional_place_name():
    r = client.post("/api/intake", json={
        "food_ids": ["pasta"], "where": "out_prepared", "where_name": "Trattoria Roma",
    })
    line = r.json()["lines"][0]
    assert line["where"] == "out_prepared"
    assert line["where_name"] == "Trattoria Roma"


def test_no_intake_line_ever_carries_a_recorded_at_field():
    r = client.post("/api/intake", json={"food_ids": ["eple", "banan"]})
    for line in r.json()["lines"]:
        stored = server.couch.get("intake", line["_id"])
        assert "recorded_at" not in stored
        assert set(stored) >= {"intake_id", "food_id", "consumed_at", "where", "author"}


def test_health_and_bathroom_and_notes_have_no_recorded_at_either():
    h = client.post("/api/health", json={"event_at": "2026-08-30T00:00:00Z", "energy": 2}).json()
    b = client.post("/api/bathroom-events", data={"kind": "pee"}).json()
    n = client.post("/api/notes", json={"text": "note"}).json()
    for doc in (h, b, n):
        assert "recorded_at" not in doc
        assert "event_at" in doc
