#!/usr/bin/env python3
"""Digestary MCP server — always part of the stack; the LLM that uses it is
optional. Reads CouchDB directly (not through the app's REST API) and
exposes a handful of write tools, every one of them gated by MCP_SECRET.

Deliberately NOT automatic: there is no background job that scans the
database for missing item emoji or empty catalog tables on every connection
— that would mean every MCP client, even one just asked an unrelated
question, pays the cost of a full `items` scan before it can answer. Filling
emoji or prefilling `items`/`symptom_items`/`bathroom_items` is a manual
step: ask the connected agent to do it (see README), and it will call
`list_items` + `update_item`/`add_item` etc. itself, on request only.

Auth: over MCP_TRANSPORT=http every request (reads included) must carry
`X-MCP-Secret: <MCP_SECRET>` — enforced by the ASGI middleware below, applied
to the whole app before any tool runs. Over stdio (an agent launches this as
a local subprocess) the process itself is already OS-trusted, so only the
write tools check the secret individually.
"""
from __future__ import annotations
import base64
import os
import re
import uuid
from datetime import datetime, timezone

import requests
from mcp.server.fastmcp import FastMCP

COUCHDB_URL = os.environ.get("COUCHDB_URL", "http://127.0.0.1:5984").rstrip("/")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "admin")
COUCHDB_PASSWORD = os.environ.get("COUCHDB_PASSWORD", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
FEVER_THRESHOLD = float(os.environ.get("FEVER_THRESHOLD", "37.8"))
LANGUAGE = os.environ.get("LANGUAGE", "en")

COUCH_AUTH = (COUCHDB_USER, COUCHDB_PASSWORD) if COUCHDB_PASSWORD else None

server = FastMCP("digestary")


# ── ISO-8601 handling (see app/couch.py for the long version of why) ────────
def normalize_iso(value, end_of_day: bool = False):
    if not value:
        return None
    s = str(value).strip()
    s = re.sub(r"(\d{2}:\d{2}:\d{2})\s+(-?\d{2}:\d{2})$", r"\1+\2", s)
    s = s.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s += "T23:59:59+00:00" if end_of_day else "T00:00:00+00:00"
    elif "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── low-level CouchDB helpers ─────────────────────────────────────────────────
def _url(db: str, doc: str | None = None, sub: str = "") -> str:
    u = f"{COUCHDB_URL}/{db}"
    if doc:
        u += f"/{doc}"
    if sub:
        u += f"/{sub}"
    return u


def mango(db: str, selector: dict, sort=None, limit: int = 2000) -> list[dict]:
    payload = {"selector": selector, "limit": limit}
    if sort:
        payload["sort"] = sort
    r = requests.post(_url(db, sub="_find"), auth=COUCH_AUTH, json=payload, timeout=15)
    if r.status_code != 200:
        r2 = requests.get(_url(db), auth=COUCH_AUTH,
                           params={"include_docs": True, "limit": limit}, timeout=15)
        return [d.get("doc", {}) for d in r2.json().get("rows", [])] if r2.status_code == 200 else []
    return r.json().get("docs", [])


def window(db: str, field: str, from_date, to_date, limit: int = 2000) -> list[dict]:
    lo, hi = normalize_iso(from_date), normalize_iso(to_date, end_of_day=True)
    rng = {}
    if lo:
        rng["$gte"] = lo
    if hi:
        rng["$lte"] = hi
    selector = {field: rng} if rng else {}
    docs = mango(db, selector, sort=[{field: "asc"}], limit=limit)
    docs = [d for d in docs if d.get(field) and (not lo or d[field] >= lo) and (not hi or d[field] <= hi)]
    docs.sort(key=lambda d: d.get(field, ""))
    return docs


def get_doc(db: str, doc_id: str) -> dict | None:
    r = requests.get(_url(db, doc_id), auth=COUCH_AUTH, timeout=10)
    return r.json() if r.status_code == 200 else None


def put_doc(db: str, doc_id: str, doc: dict) -> dict:
    payload = dict(doc)
    payload["_id"] = doc_id
    r = requests.put(_url(db, doc_id), auth=COUCH_AUTH, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def require_secret() -> None:
    """Writes require the operator to have set MCP_SECRET on this process at
    all — regardless of transport. Over HTTP the ASGI middleware below also
    checks the header against it on every request (reads included); over
    stdio, spawning this process already *is* the authorization (the
    launching agent has the same CouchDB credentials in its own env), so
    this check exists only to make 'no MCP_SECRET configured' mean 'this
    server can never write', not 'writes are silently allowed'."""
    if not MCP_SECRET:
        raise PermissionError("MCP_SECRET is not configured on this server — writes are disabled")


# ── read tools ────────────────────────────────────────────────────────────────
@server.tool()
def list_items() -> list[dict]:
    """Every food/option item (flat, equal; the user's own words — not
    English). `is_parent` is computed from `item_links`, not stored."""
    items = mango("items", {}, limit=5000)
    parents = {l["parent"] for l in mango("item_links", {}, limit=5000)}
    for it in items:
        it["is_parent"] = it.get("_id") in parents
    return items


@server.tool()
def list_item_links() -> list[dict]:
    """Sub-item -> parent suggestions (e.g. butter -> bread). UI hint only —
    an intake line always references a plain `food_id`, parent or leaf."""
    return mango("item_links", {}, limit=5000)


@server.tool()
def list_symptom_items() -> list[dict]:
    """Every symptom the user has logged, in their own words. Flat — no
    parent/child relationships for symptoms."""
    return mango("symptom_items", {}, limit=5000)


@server.tool()
def list_bathroom_items() -> list[dict]:
    """Every bathroom-event kind the user has logged, in their own words."""
    return mango("bathroom_items", {}, limit=5000)


@server.tool()
def get_intake(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Intake lines (one per selected food item) in a window, ordered by
    `consumed_at` — the time the food was actually eaten. Lines sharing an
    `intake_id` were logged as one meal. Each line: _id, intake_id, food_id
    (-> `items`), consumed_at, where ('home_prepared'|'out_prepared'),
    where_name, notes, author."""
    return window("intake", "consumed_at", from_date, to_date)


@server.tool()
def get_holidays(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Holidays/trips overlapping the window: start, stop (dates), location,
    optional name. A meal eaten away from home is separately marked by
    `where=out_prepared`+`where_name` on the intake line — holidays are for
    correlating a whole stretch of days, not a single meal."""
    lo, hi = normalize_iso(from_date) or "0000-00-00", normalize_iso(to_date, True) or "9999-99-99"
    return [h for h in mango("holidays", {}, limit=2000)
            if h.get("start", "") <= hi[:10] and h.get("stop", "") >= lo[:10]]


@server.tool()
def get_health(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Daily routines in a window (one doc per day). Each: event_at,
    temperature_celsius, energy (0-4), sleep_hours, symptoms
    ([{symptom_id}] -> symptom_items), pain_map ({region: yellow|orange|red}),
    pain_scale (0-10, optional), notes."""
    return window("health", "event_at", from_date, to_date)


@server.tool()
def get_bathroom_events(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Bathroom events in a window. Each: event_at (when it actually
    happened — reason on this, it is the only time stored), kind (->
    bathroom_items), notes, photo (true/absent — fetch bytes with
    get_bathroom_event_photo), author."""
    return window("bathroom_events", "event_at", from_date, to_date)


@server.tool()
def get_bathroom_event_photo(event_id: str) -> dict:
    """Return the optional photo attached to a bathroom event as base64, so
    a vision-capable model can read it (e.g. stool consistency). Returns
    {found, content_type, data_base64}. This is read-only — the event and
    its photo can never be modified by a model."""
    r = requests.get(_url("bathroom_events", event_id, "photo"), auth=COUCH_AUTH, timeout=20)
    if r.status_code != 200:
        return {"found": False}
    return {"found": True, "content_type": r.headers.get("content-type", "application/octet-stream"),
            "data_base64": base64.b64encode(r.content).decode()}


@server.tool()
def get_notes(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Free-text notes in a window. Each: event_at, text, author."""
    return window("notes", "event_at", from_date, to_date)


@server.tool()
def get_summary(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Aggregated stats for a window — a good first call to get the shape of
    a period before drilling into individual rows."""
    intake = window("intake", "consumed_at", from_date, to_date)
    health = window("health", "event_at", from_date, to_date)
    be = window("bathroom_events", "event_at", from_date, to_date)
    notes = window("notes", "event_at", from_date, to_date)

    temps = [h["temperature_celsius"] for h in health if h.get("temperature_celsius") is not None]
    symptom_counts: dict[str, int] = {}
    for h in health:
        for s in (h.get("symptoms") or []):
            sid = s.get("symptom_id")
            if sid:
                symptom_counts[sid] = symptom_counts.get(sid, 0) + 1
    food_counts: dict[str, int] = {}
    for line in intake:
        fid = line.get("food_id")
        if fid:
            food_counts[fid] = food_counts.get(fid, 0) + 1
    kind_counts: dict[str, int] = {}
    hour_counts: dict[int, int] = {}
    for b in be:
        k = b.get("kind")
        if k:
            kind_counts[k] = kind_counts.get(k, 0) + 1
        try:
            hour = int(b["event_at"][11:13])
            hour_counts[hour] = hour_counts.get(hour, 0) + 1
        except Exception:
            pass

    return {
        "window": {"from": from_date, "to": to_date},
        "meals": len({l["intake_id"] for l in intake}),
        "intake_lines": len(intake),
        "routines": len(health),
        "bathroom_events": len(be),
        "bathroom_event_kinds": kind_counts,
        "notes": len(notes),
        "avg_temperature": round(sum(temps) / len(temps), 2) if temps else None,
        "max_temperature": max(temps) if temps else None,
        "fever_count": sum(1 for t in temps if t >= FEVER_THRESHOLD),
        "fever_threshold": FEVER_THRESHOLD,
        "top_symptoms": sorted(symptom_counts.items(), key=lambda kv: kv[1], reverse=True)[:10],
        "top_foods": sorted(food_counts.items(), key=lambda kv: kv[1], reverse=True)[:15],
        "bathroom_events_by_hour": dict(sorted(hour_counts.items())),
    }


@server.tool()
def ask(question: str, from_date: str | None = None, to_date: str | None = None) -> dict:
    """Answer a free-form question about a window: returns the *relevant
    rows* as text (`context`) alongside the question, so a doctor or the
    user can get exactly the processing they ask for instead of a generic
    summary — e.g. "what did I eat in the 3 hours before the 13:10 bathroom
    event on the 14th?". YOU (the model) reason over `context` and produce
    the answer; this tool does not call another model. If the answer is
    worth keeping, save it with add_finding."""
    s = get_summary(from_date, to_date)
    intake = window("intake", "consumed_at", from_date, to_date)
    be = window("bathroom_events", "event_at", from_date, to_date)
    health = window("health", "event_at", from_date, to_date)
    notes = window("notes", "event_at", from_date, to_date)

    groups: dict[str, list[dict]] = {}
    for line in intake:
        groups.setdefault(line["intake_id"], []).append(line)

    lines = ["=== SUMMARY ===",
             f"meals {s['meals']} | routines {s['routines']} | bathroom events {s['bathroom_events']} | notes {s['notes']}",
             f"fevers >= {s['fever_threshold']}C: {s['fever_count']}",
             "top foods: " + ", ".join(f"{f}x{c}" for f, c in s["top_foods"]),
             "top symptoms: " + ", ".join(f"{x}x{c}" for x, c in s["top_symptoms"]),
             "", "=== MEALS (by when consumed) ==="]
    for group in sorted(groups.values(), key=lambda g: g[0]["consumed_at"]):
        foods = ", ".join(l["food_id"] for l in group)
        where = group[0].get("where", "")
        where_name = group[0].get("where_name") or ""
        lines.append(f"{group[0]['consumed_at']}: {foods} ({where}{' @ ' + where_name if where_name else ''})")

    lines += ["", "=== BATHROOM EVENTS (by when it actually happened) ==="]
    for b in be:
        note = f" — {b['notes']}" if b.get("notes") else ""
        photo = " [has photo — call get_bathroom_event_photo]" if b.get("photo") else ""
        lines.append(f"{b['event_at']}: {b.get('kind','?')}{note}{photo}")

    lines += ["", "=== DAILY ROUTINES ==="]
    for h in health:
        symptoms = ", ".join(x.get("symptom_id", "") for x in (h.get("symptoms") or []))
        pain = f" pain_scale={h['pain_scale']}" if h.get("pain_scale") is not None else ""
        lines.append(f"{h['event_at']}: temp {h.get('temperature_celsius','?')} "
                     f"energy {h.get('energy','?')} sleep {h.get('sleep_hours','?')}h "
                     f"symptoms=[{symptoms}] pain_map={h.get('pain_map') or {}}{pain}")

    lines += ["", "=== NOTES ==="]
    for n in notes:
        lines.append(f"{n['event_at']}: {n['text']}")

    return {
        "question": question,
        "window": {"from": from_date, "to": to_date},
        "context": "\n".join(lines),
        "note": ("Reason over `context` using the event times shown — every row has exactly "
                 "one time, when it actually happened, so ordering here is already correct. "
                 "Produce the answer, then call add_finding if it's worth keeping."),
    }


@server.tool()
def list_findings() -> list[dict]:
    """All saved findings and Q&A, newest first — read this before
    re-analysing, so a later session doesn't repeat work."""
    docs = mango("findings", {}, limit=2000)
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return docs


# ── write tools — every one requires MCP_SECRET ──────────────────────────────
@server.tool()
def add_finding(title: str, findings: list[str],
                 suggested_actions: list[str] | None = None, confidence: str = "low",
                 from_date: str | None = None, to_date: str | None = None,
                 question: str | None = None, answer: str | None = None) -> dict:
    """Save a correlation or Q&A. The only place a model may write besides
    the item helpers below. `confidence`: 'low'|'medium'|'high'."""
    require_secret()
    doc_id = f"f-{uuid.uuid4().hex[:16]}"
    doc = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": title, "findings": findings or [],
        "suggested_actions": suggested_actions or [], "confidence": confidence,
        "date_range": {"from": from_date, "to": to_date}, "author": "mcp",
    }
    if question:
        doc["question"] = question
    if answer:
        doc["answer"] = answer
    return put_doc("findings", doc_id, doc)


@server.tool()
def update_item(item_id: str, emoji: str) -> dict:
    """Set an item's emoji (e.g. eple -> 🍎). MANUAL — this is not run
    automatically on connection; call it only when asked to fill in emoji
    (e.g. after `list_items` shows some are missing). Additive: never
    renames or deletes the item."""
    require_secret()
    doc = get_doc("items", item_id)
    if not doc:
        raise ValueError(f"item {item_id!r} not found")
    doc["emoji"] = emoji
    return put_doc("items", item_id, doc)


@server.tool()
def add_item(name: str, emoji: str | None = None) -> dict:
    """Add one food item, in the language registered as LANGUAGE (currently
    the server's configured language). MANUAL — used e.g. when asked to
    prefill common foods for a new user; never runs on its own. Additive:
    does nothing if the name already exists as an item (case-insensitive)."""
    require_secret()
    item_id = name.strip().lower().replace(" ", "_")
    existing = get_doc("items", item_id)
    if existing:
        return existing
    return put_doc("items", item_id, {"name": name.strip(), "emoji": emoji})


@server.tool()
def add_symptom_item(name: str) -> dict:
    """Add one symptom item, in the LANGUAGE-registered language. MANUAL,
    additive — same rules as add_item."""
    require_secret()
    sid = name.strip().lower().replace(" ", "_")
    existing = get_doc("symptom_items", sid)
    if existing:
        return existing
    return put_doc("symptom_items", sid, {"name": name.strip()})


@server.tool()
def add_bathroom_item(name: str) -> dict:
    """Add one bathroom-event kind, in the LANGUAGE-registered language.
    MANUAL, additive — same rules as add_item."""
    require_secret()
    kid = name.strip().lower().replace(" ", "_")
    existing = get_doc("bathroom_items", kid)
    if existing:
        return existing
    return put_doc("bathroom_items", kid, {"name": name.strip()})


# ── entrypoint: stdio (default) or http (always-authenticated) ──────────────
def main() -> None:
    if MCP_TRANSPORT in ("http", "streamable-http", "streamable_http"):
        import uvicorn
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        class RequireMcpSecret(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if not MCP_SECRET:
                    return JSONResponse({"error": "server misconfigured: MCP_SECRET not set"}, status_code=500)
                if request.headers.get("X-MCP-Secret") != MCP_SECRET:
                    return JSONResponse({"error": "X-MCP-Secret header required"}, status_code=401)
                return await call_next(request)

        app = server.streamable_http_app()
        app.user_middleware.insert(0, Middleware(RequireMcpSecret))
        app.middleware_stack = app.build_middleware_stack()
        uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
    else:
        server.run()


if __name__ == "__main__":
    main()
