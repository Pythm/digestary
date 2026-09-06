#!/usr/bin/env python3
"""Digestary — FastAPI backend.

Serves the static (mobile-first) frontend and a JSON REST API on top of
CouchDB. Every loggable event carries exactly ONE editable time — when it
actually happened (`event_at` for routines/bathroom-events/notes,
`consumed_at` for food intake) — which defaults to now but may be moved to
the past (logging after the fact) or the future (pre-logging a packed
lunch). There is no separate "when it was entered" timestamp anywhere.

The database seeds nothing: `items`, `symptom_items`, and `bathroom_items`
start empty and grow in the user's own words as they log. An LLM connected
over MCP may optionally prefill them and fill in food emoji — see
mcp/mcp_server.py and the README; both are manual, LLM-initiated actions,
never something this server triggers on its own (kept simple on purpose:
no background job scanning the database on every MCP connection).

The LLM is always external — it talks only to the MCP server, which reads
this same CouchDB. There is no server-side LLM and no LLM_ENDPOINT here;
every feature (including "Ask") works fully with no model connected at all.

Authentication (AUTH_MODE):
   * "local" (default) -> no login, everyone is the owner. For a private
     home device.
   * "public"           -> owner actions require a logged-in session
     (see auth.py); a GUEST_TOKEN (if set) still allows add-only access
     with no account, for a one-off shared device.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from couch import Couch, normalize_iso, now_iso
from auth import AuthConfig, Principal, SESSION_COOKIE, make_auth

# ── configuration (from env) ────────────────────────────────────────────────
COUCHDB_URL = os.environ.get("COUCHDB_URL", "http://127.0.0.1:5984")
COUCHDB_USER = os.environ.get("COUCHDB_USER", "admin")
COUCHDB_PASSWORD = os.environ.get("COUCHDB_PASSWORD", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")
FEVER_THRESHOLD = float(os.environ.get("FEVER_THRESHOLD", "37.8"))
LANGUAGE = os.environ.get("LANGUAGE", "en")

couch = Couch(COUCHDB_URL, COUCHDB_USER, COUCHDB_PASSWORD)
auth_cfg = AuthConfig()
_auth = make_auth(couch, auth_cfg)
resolve_principal = _auth["resolve_principal"]

app = FastAPI(title="Digestary API", version="2.0.0")

try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[CORS_ORIGIN] if CORS_ORIGIN != "*" else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    pass


def owner_dep(p: Principal = Depends(resolve_principal)) -> Principal:
    return _auth["require_owner"](p)


def author_of(p: Principal) -> str:
    return p.username if p.role == "owner" else f"guest:{p.username}"


# ── request models ──────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


class CreateUserIn(BaseModel):
    username: str
    password: str


class ItemIn(BaseModel):
    name: str
    emoji: Optional[str] = None


class ItemLinkIn(BaseModel):
    child: str
    parent: str


class IntakeIn(BaseModel):
    food_ids: list[str]
    consumed_at: Optional[str] = None
    where: str = "home_prepared"        # home_prepared | out_prepared
    where_name: Optional[str] = None
    notes: Optional[str] = ""


class IntakePatchIn(BaseModel):
    consumed_at: Optional[str] = None
    where: Optional[str] = None
    where_name: Optional[str] = None
    notes: Optional[str] = None
    food_id: Optional[str] = None       # line-level only


class HolidayIn(BaseModel):
    start: str
    stop: str
    location: str
    name: Optional[str] = None


class HealthIn(BaseModel):
    event_at: Optional[str] = None
    temperature_celsius: Optional[float] = None
    energy: Optional[int] = None
    sleep_hours: Optional[float] = None
    symptom_ids: Optional[list[str]] = None
    pain_map: Optional[dict[str, str]] = None
    pain_scale: Optional[int] = None
    notes: Optional[str] = None


class NoteIn(BaseModel):
    event_at: Optional[str] = None
    text: str


class AskIn(BaseModel):
    question: str
    frm: Optional[str] = None
    to: Optional[str] = None
    save: bool = False


WHERE_VALUES = ("home_prepared", "out_prepared")


# ── config / status ──────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    return {
        "app_name": "Digestary",
        "auth_mode": auth_cfg.mode,
        "guest_enabled": bool(auth_cfg.guest_token),
        "fever_threshold": FEVER_THRESHOLD,
        "language": LANGUAGE,
    }


@app.get("/api/health-check")
def health_check():
    ok = couch.ping()
    return {"status": "ok" if ok else "degraded", "couchdb": ok}


# ── auth ──────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    if auth_cfg.mode != "public":
        raise HTTPException(status_code=400, detail="login is only used in AUTH_MODE=public")
    token = _auth["login"](body.username, body.password)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                         secure=auth_cfg.cookie_secure, max_age=auth_cfg.session_days * 86400)
    return {"username": body.username.lower().strip(), "role": "owner"}


@app.post("/api/auth/logout")
def logout(response: Response, everywhere: bool = False,
           p: Principal = Depends(resolve_principal)):
    if everywhere and p.role == "owner":
        _auth["logout_everywhere"](p.username)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(p: Principal = Depends(resolve_principal)):
    return {"username": p.username, "role": p.role}


@app.post("/api/auth/users")
def create_user(body: CreateUserIn, p: Principal = Depends(owner_dep)):
    """Add another owner account to this household's stack (e.g. a partner
    who wants their own login). Guest access stays the shared GUEST_TOKEN —
    this endpoint is only for additional full (owner) accounts."""
    return {k: v for k, v in _auth["create_user"](body.username, body.password).items()
            if k not in ("password_hash", "salt")}


# ── items (food catalog — flat, equal; empty until the user logs) ──────────
def _linked_parent_ids() -> set[str]:
    return {l["parent"] for l in couch.mango("item_links", {}, limit=5000)}


@app.get("/api/items")
def list_items():
    items = couch.mango("items", {}, limit=5000)
    parents = _linked_parent_ids()
    for it in items:
        it["is_parent"] = it.get("_id") in parents
    return items


@app.get("/api/items/{item_id}")
def get_item(item_id: str):
    doc = couch.get("items", item_id)
    if not doc:
        raise HTTPException(status_code=404, detail="item not found")
    doc["is_parent"] = item_id in _linked_parent_ids()
    return doc


@app.post("/api/items")
def create_item(body: ItemIn, p: Principal = Depends(resolve_principal)):
    if auth_cfg.mode == "public" and p.role != "owner":
        raise HTTPException(status_code=403, detail="only the owner can add new food items")
    item_id = body.name.strip().lower().replace(" ", "_")
    if not item_id:
        raise HTTPException(status_code=400, detail="name required")
    existing = couch.get("items", item_id)
    if existing:
        return existing
    doc = {"name": body.name.strip(), "emoji": body.emoji, "created_at": now_iso()}
    return couch.put("items", item_id, doc)


@app.patch("/api/items/{item_id}")
def update_item_emoji(item_id: str, body: dict,
                       x_mcp_secret: Optional[str] = Header(default=None, alias="X-MCP-Secret")):
    """The only way an item's emoji is set — an LLM, connected over MCP with
    the write secret, fills it in (see README: 'filling in food emoji').
    Additive only: cannot rename or delete the item."""
    if not MCP_SECRET or x_mcp_secret != MCP_SECRET:
        raise HTTPException(status_code=401, detail="X-MCP-Secret required")
    doc = couch.get("items", item_id)
    if not doc:
        raise HTTPException(status_code=404, detail="item not found")
    if "emoji" in body:
        doc["emoji"] = body["emoji"]
    return couch.put("items", item_id, doc)


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, p: Principal = Depends(owner_dep)):
    couch.delete("items", item_id)
    return {"deleted": item_id}


# ── item links (sub-item -> parent; UI suggestion only) ─────────────────────
@app.get("/api/item-links")
def list_item_links():
    return couch.mango("item_links", {}, limit=5000)


@app.post("/api/item-links")
def create_item_link(body: ItemLinkIn, p: Principal = Depends(resolve_principal)):
    if auth_cfg.mode == "public" and p.role != "owner":
        raise HTTPException(status_code=403, detail="only the owner can manage the catalog")
    link_id = f"link-{body.child}-{body.parent}"
    return couch.put("item_links", link_id, {"child": body.child, "parent": body.parent})


@app.delete("/api/item-links/{link_id}")
def delete_item_link(link_id: str, p: Principal = Depends(owner_dep)):
    couch.delete("item_links", link_id)
    return {"deleted": link_id}


# ── symptom items (flat list, no sub-options) ────────────────────────────────
@app.get("/api/symptom-items")
def list_symptom_items():
    return couch.mango("symptom_items", {}, limit=5000)


@app.post("/api/symptom-items")
def create_symptom_item(body: ItemIn, p: Principal = Depends(resolve_principal)):
    sid = body.name.strip().lower().replace(" ", "_")
    if not sid:
        raise HTTPException(status_code=400, detail="name required")
    existing = couch.get("symptom_items", sid)
    if existing:
        return existing
    return couch.put("symptom_items", sid, {"name": body.name.strip()})


@app.delete("/api/symptom-items/{sid}")
def delete_symptom_item(sid: str, p: Principal = Depends(owner_dep)):
    couch.delete("symptom_items", sid)
    return {"deleted": sid}


# ── bathroom items (flat list, no emoji) ─────────────────────────────────────
@app.get("/api/bathroom-items")
def list_bathroom_items():
    return couch.mango("bathroom_items", {}, limit=5000)


@app.post("/api/bathroom-items")
def create_bathroom_item(body: ItemIn, p: Principal = Depends(resolve_principal)):
    kid = body.name.strip().lower().replace(" ", "_")
    if not kid:
        raise HTTPException(status_code=400, detail="name required")
    existing = couch.get("bathroom_items", kid)
    if existing:
        return existing
    return couch.put("bathroom_items", kid, {"name": body.name.strip()})


@app.delete("/api/bathroom-items/{kid}")
def delete_bathroom_item(kid: str, p: Principal = Depends(owner_dep)):
    couch.delete("bathroom_items", kid)
    return {"deleted": kid}


# ── intake — one line per selected food_id, grouped by intake_id ────────────
@app.post("/api/intake")
def create_intake(body: IntakeIn, p: Principal = Depends(resolve_principal)):
    if body.where not in WHERE_VALUES:
        raise HTTPException(status_code=400, detail=f"where must be one of {WHERE_VALUES}")
    if not body.food_ids:
        raise HTTPException(status_code=400, detail="food_ids must not be empty")
    consumed_at = normalize_iso(body.consumed_at) or now_iso()
    intake_id = f"i-{uuid.uuid4().hex[:12]}"
    author = author_of(p)
    lines = []
    for food_id in body.food_ids:
        line_id = f"il-{uuid.uuid4().hex[:12]}"
        doc = {
            "intake_id": intake_id,
            "food_id": food_id,
            "consumed_at": consumed_at,
            "where": body.where,
            "where_name": body.where_name,
            "notes": body.notes or "",
            "author": author,
        }
        lines.append(couch.put("intake", line_id, doc))
    return {"intake_id": intake_id, "consumed_at": consumed_at, "lines": lines}


@app.get("/api/intake")
def list_intake(frm: Optional[str] = None, to: Optional[str] = None):
    return couch.window("intake", "consumed_at", normalize_iso(frm), normalize_iso(to, True), order="desc")


@app.get("/api/intake/group/{intake_id}")
def get_intake_group(intake_id: str):
    lines = couch.mango("intake", {"intake_id": intake_id}, limit=200)
    if not lines:
        raise HTTPException(status_code=404, detail="intake not found")
    return {"intake_id": intake_id, "lines": lines}


@app.patch("/api/intake/line/{line_id}")
def patch_intake_line(line_id: str, body: IntakePatchIn,
                       p: Principal = Depends(resolve_principal)):
    doc = couch.get("intake", line_id)
    if not doc:
        raise HTTPException(status_code=404, detail="intake line not found")
    if body.consumed_at is not None:
        doc["consumed_at"] = normalize_iso(body.consumed_at)
    if body.where is not None:
        if body.where not in WHERE_VALUES:
            raise HTTPException(status_code=400, detail=f"where must be one of {WHERE_VALUES}")
        doc["where"] = body.where
    if body.where_name is not None:
        doc["where_name"] = body.where_name
    if body.notes is not None:
        doc["notes"] = body.notes
    if body.food_id is not None:
        doc["food_id"] = body.food_id
    return couch.put("intake", line_id, doc)


@app.patch("/api/intake/group/{intake_id}")
def patch_intake_group(intake_id: str, body: IntakePatchIn,
                        p: Principal = Depends(resolve_principal)):
    """Move/edit the shared fields (typically consumed_at) across every line
    of one logged meal at once — the 'go back and fix the time' path."""
    lines = couch.mango("intake", {"intake_id": intake_id}, limit=200)
    if not lines:
        raise HTTPException(status_code=404, detail="intake not found")
    updated = []
    for doc in lines:
        if body.consumed_at is not None:
            doc["consumed_at"] = normalize_iso(body.consumed_at)
        if body.where is not None:
            if body.where not in WHERE_VALUES:
                raise HTTPException(status_code=400, detail=f"where must be one of {WHERE_VALUES}")
            doc["where"] = body.where
        if body.where_name is not None:
            doc["where_name"] = body.where_name
        if body.notes is not None:
            doc["notes"] = body.notes
        updated.append(couch.put("intake", doc["_id"], doc))
    return {"intake_id": intake_id, "lines": updated}


@app.delete("/api/intake/line/{line_id}")
def delete_intake_line(line_id: str, p: Principal = Depends(owner_dep)):
    couch.delete("intake", line_id)
    return {"deleted": line_id}


@app.delete("/api/intake/group/{intake_id}")
def delete_intake_group(intake_id: str, p: Principal = Depends(owner_dep)):
    lines = couch.mango("intake", {"intake_id": intake_id}, limit=200)
    for doc in lines:
        couch.delete("intake", doc["_id"])
    return {"deleted": intake_id, "lines": len(lines)}


# ── holidays (a range: start -> stop) ────────────────────────────────────────
@app.post("/api/holidays")
def create_holiday(body: HolidayIn, p: Principal = Depends(resolve_principal)):
    hid = f"h-{uuid.uuid4().hex[:12]}"
    doc = {"start": body.start, "stop": body.stop, "location": body.location,
           "name": body.name, "author": author_of(p)}
    return couch.put("holidays", hid, doc)


@app.get("/api/holidays")
def list_holidays(frm: Optional[str] = None, to: Optional[str] = None):
    holidays = couch.mango("holidays", {}, limit=2000)
    if not frm and not to:
        return holidays
    frm_d, to_d = (frm or "0000-00-00"), (to or "9999-99-99")
    return [h for h in holidays if h.get("start", "") <= to_d and h.get("stop", "") >= frm_d]


@app.get("/api/holidays/{hid}")
def get_holiday(hid: str):
    doc = couch.get("holidays", hid)
    if not doc:
        raise HTTPException(status_code=404, detail="holiday not found")
    return doc


@app.patch("/api/holidays/{hid}")
def patch_holiday(hid: str, body: dict, p: Principal = Depends(resolve_principal)):
    doc = couch.get("holidays", hid)
    if not doc:
        raise HTTPException(status_code=404, detail="holiday not found")
    for k in ("start", "stop", "location", "name"):
        if k in body:
            doc[k] = body[k]
    return couch.put("holidays", hid, doc)


@app.delete("/api/holidays/{hid}")
def delete_holiday(hid: str, p: Principal = Depends(owner_dep)):
    couch.delete("holidays", hid)
    return {"deleted": hid}


# ── health / daily routine (upsert by date) ──────────────────────────────────
@app.post("/api/health")
def upsert_health(body: HealthIn, p: Principal = Depends(resolve_principal)):
    event_iso = normalize_iso(body.event_at) or now_iso()
    date_key = event_iso[:10]
    doc_id = f"health-{date_key}"
    existing = couch.get("health", doc_id) or {}
    for k in ("temperature_celsius", "energy", "sleep_hours", "pain_map",
              "pain_scale", "notes"):
        val = getattr(body, k)
        if val is not None:
            existing[k] = val
    if body.symptom_ids is not None:
        existing["symptoms"] = [{"symptom_id": sid} for sid in body.symptom_ids]
    existing["event_at"] = event_iso
    existing.setdefault("author", author_of(p))
    return couch.put("health", doc_id, existing)


@app.get("/api/health")
def list_health(date: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None):
    if date:
        doc = couch.get("health", f"health-{date}")
        return [doc] if doc else []
    return couch.window("health", "event_at", normalize_iso(frm), normalize_iso(to, True), order="desc")


@app.patch("/api/health/{doc_id}")
def patch_health(doc_id: str, body: dict, p: Principal = Depends(resolve_principal)):
    doc = couch.get("health", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if "event_at" in body and body["event_at"]:
        doc["event_at"] = normalize_iso(body["event_at"])
    for k in ("temperature_celsius", "energy", "sleep_hours", "pain_map",
              "pain_scale", "notes"):
        if k in body:
            doc[k] = body[k]
    if "symptom_ids" in body:
        doc["symptoms"] = [{"symptom_id": sid} for sid in body["symptom_ids"]]
    return couch.put("health", doc_id, doc)


# ── bathroom events (kind refers to bathroom_items; optional photo) ─────────
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


async def _store_photo(doc_id: str, photo: Optional[UploadFile]) -> dict:
    if not photo or not photo.filename:
        return {}
    if photo.content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(status_code=400, detail="photo must be jpeg, png, or webp")
    data = await photo.read()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=400, detail="photo must be 5 MB or smaller")
    couch.put_attachment("bathroom_events", doc_id, "photo", data, photo.content_type)
    return {"photo": True, "photo_content_type": photo.content_type}


@app.post("/api/bathroom-events")
async def create_bathroom_event(
    kind: str = Form(...),
    event_at: Optional[str] = Form(None),
    notes: Optional[str] = Form(""),
    photo: Optional[UploadFile] = File(None),
    p: Principal = Depends(resolve_principal),
):
    doc_id = f"b-{uuid.uuid4().hex[:12]}"
    doc = {
        "event_at": normalize_iso(event_at) or now_iso(),
        "kind": kind,
        "notes": notes or "",
        "author": author_of(p),
    }
    saved = couch.put("bathroom_events", doc_id, doc)
    photo_meta = await _store_photo(doc_id, photo)
    if photo_meta:
        saved.update(photo_meta)
        saved = couch.put("bathroom_events", doc_id, saved)
    return saved


@app.get("/api/bathroom-events")
def list_bathroom_events(date: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None):
    if date:
        rows = couch.mango("bathroom_events", {}, limit=2000)
        return [r for r in rows if str(r.get("event_at", "")).startswith(date)]
    return couch.window("bathroom_events", "event_at", normalize_iso(frm), normalize_iso(to, True), order="desc")


@app.patch("/api/bathroom-events/{doc_id}")
async def patch_bathroom_event(
    doc_id: str,
    kind: Optional[str] = Form(None),
    event_at: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    p: Principal = Depends(resolve_principal),
):
    doc = couch.get("bathroom_events", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if kind is not None:
        doc["kind"] = kind
    if event_at is not None:
        doc["event_at"] = normalize_iso(event_at)
    if notes is not None:
        doc["notes"] = notes
    doc = couch.put("bathroom_events", doc_id, doc)
    photo_meta = await _store_photo(doc_id, photo)
    if photo_meta:
        doc.update(photo_meta)
        doc = couch.put("bathroom_events", doc_id, doc)
    return doc


@app.get("/api/bathroom-events/{doc_id}/photo")
def get_bathroom_event_photo(doc_id: str, p: Principal = Depends(resolve_principal)):
    result = couch.get_attachment("bathroom_events", doc_id, "photo")
    if not result:
        raise HTTPException(status_code=404, detail="no photo for this event")
    data, ctype = result
    from fastapi.responses import Response as FastResponse
    return FastResponse(content=data, media_type=ctype)


@app.delete("/api/bathroom-events/{doc_id}/photo")
def delete_bathroom_event_photo(doc_id: str, p: Principal = Depends(owner_dep)):
    doc = couch.get("bathroom_events", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    doc.pop("photo", None)
    doc.pop("photo_content_type", None)
    couch.put("bathroom_events", doc_id, doc)
    return {"ok": True}


@app.delete("/api/bathroom-events/{doc_id}")
def delete_bathroom_event(doc_id: str, p: Principal = Depends(owner_dep)):
    couch.delete("bathroom_events", doc_id)
    return {"deleted": doc_id}


# ── notes ─────────────────────────────────────────────────────────────────────
@app.post("/api/notes")
def create_note(body: NoteIn, p: Principal = Depends(resolve_principal)):
    doc_id = f"n-{uuid.uuid4().hex[:12]}"
    doc = {"event_at": normalize_iso(body.event_at) or now_iso(),
           "text": body.text, "author": author_of(p)}
    return couch.put("notes", doc_id, doc)


@app.get("/api/notes")
def list_notes(date: Optional[str] = None, frm: Optional[str] = None, to: Optional[str] = None):
    if date:
        rows = couch.mango("notes", {}, limit=2000)
        return [r for r in rows if str(r.get("event_at", "")).startswith(date)]
    return couch.window("notes", "event_at", normalize_iso(frm), normalize_iso(to, True), order="desc")


@app.patch("/api/notes/{doc_id}")
def patch_note(doc_id: str, body: dict, p: Principal = Depends(resolve_principal)):
    doc = couch.get("notes", doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if "event_at" in body and body["event_at"]:
        doc["event_at"] = normalize_iso(body["event_at"])
    if "text" in body:
        doc["text"] = body["text"]
    return couch.put("notes", doc_id, doc)


@app.delete("/api/notes/{doc_id}")
def delete_note(doc_id: str, p: Principal = Depends(owner_dep)):
    couch.delete("notes", doc_id)
    return {"deleted": doc_id}


# ── findings — read by anyone, write only by an MCP client or the owner ─────
@app.get("/api/findings")
def list_findings():
    return couch.mango("findings", {}, sort=[{"created_at": "desc"}], limit=500)


@app.post("/api/findings")
def create_finding(
    body: dict,
    request: Request,
    x_mcp_secret: Optional[str] = Header(default=None, alias="X-MCP-Secret"),
    x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token"),
):
    """Only an MCP caller (with the secret) or the owner may write a finding
    — this is the ONLY writable surface for a connected LLM, besides the
    item-emoji PATCH. Independent of X-Auth-Token: an MCP client authenticates
    with the secret alone and must not also need a web session."""
    is_mcp = bool(MCP_SECRET) and x_mcp_secret == MCP_SECRET
    principal = None if is_mcp else _auth["try_resolve_principal"](
        x_auth_token, request.cookies.get(SESSION_COOKIE))
    is_owner = bool(principal) and principal.role == "owner"
    if not (is_mcp or is_owner):
        raise HTTPException(status_code=401,
                             detail="only an MCP client (X-MCP-Secret) or the owner may write findings")
    finding = dict(body)
    doc_id = finding.get("_id") or f"f-{uuid.uuid4().hex[:16]}"
    finding["_id"] = doc_id
    finding["created_at"] = now_iso()
    finding.setdefault("author", "mcp" if is_mcp else "owner")
    return couch.put("findings", doc_id, finding)


# ── summary / aggregation ────────────────────────────────────────────────────
@app.get("/api/summary")
def summary(frm: Optional[str] = None, to: Optional[str] = None):
    lo, hi = normalize_iso(frm), normalize_iso(to, True)
    intake = couch.window("intake", "consumed_at", lo, hi, order="asc", limit=5000)
    health = couch.window("health", "event_at", lo, hi, order="asc", limit=2000)
    bevents = couch.window("bathroom_events", "event_at", lo, hi, order="asc", limit=2000)
    notes = couch.window("notes", "event_at", lo, hi, order="asc", limit=2000)

    temps = [h["temperature_celsius"] for h in health if h.get("temperature_celsius") is not None]
    fever_count = sum(1 for t in temps if t >= FEVER_THRESHOLD)

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
    for b in bevents:
        k = b.get("kind")
        if k:
            kind_counts[k] = kind_counts.get(k, 0) + 1
        try:
            hour = int(b["event_at"][11:13])
            hour_counts[hour] = hour_counts.get(hour, 0) + 1
        except Exception:
            pass

    return {
        "window": {"from": frm, "to": to},
        "intake_lines": len(intake),
        "daily_routine_count": len(health),
        "bathroom_event_count": len(bevents),
        "bathroom_event_kinds": kind_counts,
        "avg_temperature": round(sum(temps) / len(temps), 2) if temps else None,
        "max_temperature": max(temps) if temps else None,
        "fever_count": fever_count,
        "fever_threshold": FEVER_THRESHOLD,
        "top_symptoms": sorted(symptom_counts.items(), key=lambda kv: kv[1], reverse=True)[:8],
        "top_foods": sorted(food_counts.items(), key=lambda kv: kv[1], reverse=True)[:10],
        "bathroom_event_hours": dict(sorted(hour_counts.items())),
        "notes_count": len(notes),
    }


# ── timeline — point events sorted by their happened-time, holidays as bands ─
@app.get("/api/timeline")
def timeline(frm: Optional[str] = None, to: Optional[str] = None):
    lo, hi = normalize_iso(frm), normalize_iso(to, True)
    intake = couch.window("intake", "consumed_at", lo, hi, order="asc", limit=5000)
    groups: dict[str, list[dict]] = {}
    for line in intake:
        groups.setdefault(line["intake_id"], []).append(line)

    items = []
    for intake_id, lines in groups.items():
        items.append({"type": "meal", "event_at": lines[0]["consumed_at"],
                       "doc": {"intake_id": intake_id, "lines": lines}})
    for doc in couch.window("health", "event_at", lo, hi, order="asc", limit=2000):
        items.append({"type": "routine", "event_at": doc["event_at"], "doc": doc})
    for doc in couch.window("bathroom_events", "event_at", lo, hi, order="asc", limit=2000):
        items.append({"type": "bathroom", "event_at": doc["event_at"], "doc": doc})
    for doc in couch.window("notes", "event_at", lo, hi, order="asc", limit=2000):
        items.append({"type": "note", "event_at": doc["event_at"], "doc": doc})
    items.sort(key=lambda x: x["event_at"])

    holidays = list_holidays(frm, to)
    return {"items": items, "holidays": holidays}


# ── ask — always returns the relevant rows; no server-side LLM ─────────────
def _format_ask_context(frm: Optional[str], to: Optional[str]) -> str:
    lo, hi = normalize_iso(frm), normalize_iso(to, True)
    intake = couch.window("intake", "consumed_at", lo, hi, order="asc", limit=500)
    bevents = couch.window("bathroom_events", "event_at", lo, hi, order="asc", limit=500)
    health = couch.window("health", "event_at", lo, hi, order="asc", limit=500)
    notes = couch.window("notes", "event_at", lo, hi, order="asc", limit=500)

    groups: dict[str, list[dict]] = {}
    for line in intake:
        groups.setdefault(line["intake_id"], []).append(line)

    lines = ["=== MEALS (by when consumed) ==="]
    for intake_id, group in sorted(groups.items(), key=lambda kv: kv[1][0]["consumed_at"]):
        foods = ", ".join(l["food_id"] for l in group)
        where = group[0].get("where", "")
        where_name = group[0].get("where_name") or ""
        lines.append(f"{group[0]['consumed_at']}: {foods} ({where}{' @ ' + where_name if where_name else ''})")

    lines.append("")
    lines.append("=== BATHROOM EVENTS ===")
    for b in bevents:
        note = f" — {b['notes']}" if b.get("notes") else ""
        lines.append(f"{b['event_at']}: {b.get('kind','?')}{note}")

    lines.append("")
    lines.append("=== DAILY ROUTINE ===")
    for h in health:
        symptoms = ", ".join(s.get("symptom_id", "") for s in (h.get("symptoms") or []))
        lines.append(
            f"{h['event_at']}: temp {h.get('temperature_celsius','?')} "
            f"energy {h.get('energy','?')} sleep {h.get('sleep_hours','?')}h "
            f"symptoms=[{symptoms}] pain_scale={h.get('pain_scale','?')} "
            f"pain_map={h.get('pain_map') or {}}"
        )

    lines.append("")
    lines.append("=== NOTES ===")
    for n in notes:
        lines.append(f"{n['event_at']}: {n['text']}")

    return "\n".join(lines)


@app.post("/api/ask")
def ask(body: AskIn, p: Principal = Depends(resolve_principal)):
    context = _format_ask_context(body.frm, body.to)
    if body.save and p.role == "owner":
        doc_id = f"f-{uuid.uuid4().hex[:16]}"
        couch.put("findings", doc_id, {
            "created_at": now_iso(),
            "question": body.question,
            "answer": None,
            "date_range": {"from": body.frm, "to": body.to},
            "author": author_of(p),
        })
    return {"question": body.question, "window": {"from": body.frm, "to": body.to}, "context": context}


# ── mount the static frontend ────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
