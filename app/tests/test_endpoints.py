"""Endpoint smoke tests (AUTH_MODE=local) against the in-memory fake CouchDB.

Exercises the real endpoint logic: item is_parent derivation, the single
event-time invariant, intake line/group semantics, holidays as a range,
the pain-map health doc, bathroom events + photo attachments, and findings
write-gating.
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
fake = install_fake(couch_module)


def setup_function(_):
    global fake
    fake = install_fake(couch_module)


def test_config_and_health_check():
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["auth_mode"] == "local"
    r = client.get("/api/health-check")
    assert r.status_code == 200
    assert r.json()["couchdb"] is True


def test_item_is_parent_is_derived_not_stored():
    client.post("/api/items", json={"name": "brød"})
    client.post("/api/items", json={"name": "smør"})
    r = client.get("/api/items")
    by_id = {i["_id"]: i for i in r.json()}
    assert by_id["brød"]["is_parent"] is False  # no link yet
    assert "is_parent" not in server.couch.get("items", "brød")  # never stored

    client.post("/api/item-links", json={"child": "smør", "parent": "brød"})
    r = client.get("/api/items")
    by_id = {i["_id"]: i for i in r.json()}
    assert by_id["brød"]["is_parent"] is True
    assert by_id["smør"]["is_parent"] is False


def test_intake_one_line_per_food_shares_intake_id_and_has_single_timestamp():
    client.post("/api/items", json={"name": "brød"})
    client.post("/api/items", json={"name": "smør"})
    r = client.post("/api/intake", json={
        "food_ids": ["brød", "smør"],
        "consumed_at": "2026-08-30T08:15:00+00:00",
        "where": "home_prepared",
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["lines"]) == 2
    intake_id = body["intake_id"]
    for line in body["lines"]:
        assert line["intake_id"] == intake_id
        assert line["consumed_at"] == "2026-08-30T08:15:00Z"
        assert "recorded_at" not in line  # the two-timestamp idea is retired


def test_intake_group_patch_moves_every_line_and_line_patch_moves_one():
    r = client.post("/api/intake", json={"food_ids": ["eple", "vann"],
                                          "consumed_at": "2026-08-30T08:00:00Z"})
    intake_id = r.json()["intake_id"]
    lines = r.json()["lines"]

    client.patch(f"/api/intake/group/{intake_id}", json={"consumed_at": "2026-08-30T09:00:00Z"})
    group = client.get(f"/api/intake/group/{intake_id}").json()
    assert all(l["consumed_at"] == "2026-08-30T09:00:00Z" for l in group["lines"])

    one_line_id = lines[0]["_id"]
    client.patch(f"/api/intake/line/{one_line_id}", json={"notes": "felt fine"})
    line = server.couch.get("intake", one_line_id)
    assert line["notes"] == "felt fine"
    other_line_id = lines[1]["_id"]
    assert server.couch.get("intake", other_line_id)["notes"] == ""


def test_intake_where_must_be_valid():
    r = client.post("/api/intake", json={"food_ids": ["eple"], "where": "at_a_friends"})
    assert r.status_code == 400


def test_intake_delete_line_vs_delete_whole_group():
    r = client.post("/api/intake", json={"food_ids": ["a", "b", "c"]})
    intake_id = r.json()["intake_id"]
    line_id = r.json()["lines"][0]["_id"]

    client.delete(f"/api/intake/line/{line_id}")
    remaining = client.get(f"/api/intake/group/{intake_id}").json()["lines"]
    assert len(remaining) == 2

    client.delete(f"/api/intake/group/{intake_id}")
    r2 = client.get(f"/api/intake/group/{intake_id}")
    assert r2.status_code == 404


def test_holidays_are_a_range_and_overlap_filter_works():
    client.post("/api/holidays", json={"start": "2026-08-10", "stop": "2026-08-12",
                                        "location": "Rome, Italy", "name": "Rome trip"})
    r = client.get("/api/holidays?frm=2026-08-11&to=2026-08-20")
    assert len(r.json()) == 1
    r2 = client.get("/api/holidays?frm=2026-09-01&to=2026-09-05")
    assert len(r2.json()) == 0


def test_health_upsert_by_date_with_pain_map_and_symptoms():
    client.post("/api/symptom-items", json={"name": "magesmerter"})
    client.post("/api/health", json={
        "event_at": "2026-08-30T00:00:00Z", "temperature_celsius": 37.2,
        "energy": 2, "symptom_ids": ["magesmerter"],
        "pain_map": {"abdomen_lower_left": "red"}, "pain_scale": 6,
    })
    r = client.get("/api/health?date=2026-08-30")
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["symptoms"] == [{"symptom_id": "magesmerter"}]
    assert rows[0]["pain_map"] == {"abdomen_lower_left": "red"}
    assert "recorded_at" not in rows[0]

    # second save same day updates, does not duplicate
    client.post("/api/health", json={"event_at": "2026-08-30T00:00:00Z", "temperature_celsius": 38.1})
    rows2 = client.get("/api/health?date=2026-08-30").json()
    assert len(rows2) == 1
    assert rows2[0]["temperature_celsius"] == 38.1
    assert rows2[0]["pain_scale"] == 6  # untouched fields survive the upsert


def test_bathroom_event_kind_is_free_text_backed_by_bathroom_items():
    client.post("/api/bathroom-items", json={"name": "pee"})
    r = client.post("/api/bathroom-events", data={"kind": "pee",
                                                    "event_at": "2026-08-30T13:10:00Z",
                                                    "notes": "rushed"})
    assert r.status_code == 200
    assert r.json()["kind"] == "pee"
    assert "recorded_at" not in r.json()


def test_bathroom_event_photo_upload_and_fetch():
    r = client.post("/api/bathroom-events", data={"kind": "poo", "event_at": ""},
                     files={"photo": ("stool.jpg", b"\xff\xd8\xfake-jpeg-bytes", "image/jpeg")})
    assert r.status_code == 200
    doc_id = r.json()["_id"]
    assert r.json()["photo"] is True

    photo = client.get(f"/api/bathroom-events/{doc_id}/photo")
    assert photo.status_code == 200
    assert photo.content == b"\xff\xd8\xfake-jpeg-bytes"
    assert photo.headers["content-type"] == "image/jpeg"


def test_bathroom_event_photo_rejects_bad_content_type():
    r = client.post("/api/bathroom-events", data={"kind": "poo", "event_at": ""},
                     files={"photo": ("doc.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400


def test_notes_have_single_event_time():
    r = client.post("/api/notes", json={"text": "started a new supplement",
                                         "event_at": "2026-08-30T20:00:00Z"})
    assert r.status_code == 200
    assert "recorded_at" not in r.json()


def test_summary_counts_intake_lines_and_bathroom_kinds():
    client.post("/api/intake", json={"food_ids": ["brød", "smør"],
                                      "consumed_at": "2026-08-30T08:15:00Z"})
    client.post("/api/bathroom-events", data={"kind": "poo", "event_at": "2026-08-30T09:00:00Z"})
    r = client.get("/api/summary?frm=2026-08-30T00:00:00Z&to=2026-08-30T23:59:59Z")
    s = r.json()
    assert s["intake_lines"] >= 2
    assert s["bathroom_event_kinds"].get("poo", 0) >= 1


def test_timeline_groups_meals_and_sorts_by_event_time():
    client.post("/api/intake", json={"food_ids": ["ris"], "consumed_at": "2026-08-30T20:00:00Z"})
    client.post("/api/bathroom-events", data={"kind": "pee", "event_at": "2026-08-30T06:00:00Z"})
    r = client.get("/api/timeline?frm=2026-08-30T00:00:00Z&to=2026-08-30T23:59:59Z")
    items = r.json()["items"]
    times = [x["event_at"] for x in items]
    assert times == sorted(times)
    assert any(x["type"] == "meal" and "lines" in x["doc"] for x in items)


def test_ask_returns_context_rows_with_no_server_side_llm():
    client.post("/api/intake", json={"food_ids": ["eple"], "consumed_at": "2026-08-30T08:00:00Z"})
    r = client.post("/api/ask", json={"question": "what did I eat?",
                                       "frm": "2026-08-30T00:00:00Z", "to": "2026-08-30T23:59:59Z"})
    assert r.status_code == 200
    assert "eple" in r.json()["context"]


def test_findings_writable_by_mcp_secret_or_by_the_local_owner():
    # a correct MCP secret writes as "mcp", attributing the write to the model
    r = client.post("/api/findings", json={"title": "correlation", "findings": ["a"]},
                     headers={"X-MCP-Secret": "test-mcp-secret"})
    assert r.status_code == 200
    assert r.json()["author"] == "mcp"

    # in AUTH_MODE=local everyone is the owner (no login concept at all), so
    # a plain write with no secret still succeeds, attributed to the owner —
    # the secret only matters once AUTH_MODE=public (see test_auth.py)
    r2 = client.post("/api/findings", json={"title": "manual note", "findings": []})
    assert r2.status_code == 200
    assert r2.json()["author"] == "owner"

    titles = {f["title"] for f in client.get("/api/findings").json()}
    assert {"correlation", "manual note"} <= titles
