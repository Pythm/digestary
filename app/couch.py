"""Low-level CouchDB HTTP helpers shared by server.py.

Two things learned from the first (pre-plan) build, kept here on purpose:

1. CouchDB's Mango `_find` compares values the way JSON/JS does — lexicographic
   for strings, numeric for numbers. Comparing an ISO-8601 string against a
   Unix-timestamp float with `$gte`/`$lte` therefore matches nothing. The fix
   used here is to always store event times as a single canonical string shape
   — `YYYY-MM-DDTHH:MM:SSZ`, UTC, zero-padded, no microseconds — so plain
   string comparison IS chronological comparison, and a real Mango range query
   works (see `_index` creation in init_db.py).
2. A `+` in a timezone offset can arrive as a literal space in a query string
   (`...T23:59:59+00:00` -> `...T23:59:59 00:00`) because Starlette decodes
   `+` to a space per the `application/x-www-form-urlencoded` convention, even
   though this is a query string, not a form body. `normalize_iso` repairs it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests


def normalize_iso(value: Optional[str], end_of_day: bool = False) -> Optional[str]:
    """Parse a loose ISO-8601 / date string and return the canonical
    `YYYY-MM-DDTHH:MM:SSZ` (UTC) form used for storage and range queries.

    `end_of_day=True` turns a bare date into 23:59:59 instead of 00:00:00,
    so a `to=2026-08-30` window bound is inclusive of that whole day.
    """
    if not value:
        return None
    s = str(value).strip()
    # repair a '+' offset turned into a space by query-string decoding
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
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Couch:
    """A thin CouchDB client. Holds the base URL/auth; every call is a plain
    HTTP request via the `requests` module-level import (tests monkeypatch
    this module's `requests` attribute, see tests/fake_couch.py)."""

    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (user, password) if password else None

    def _path(self, db: str, doc_id: Optional[str] = None, sub: str = "") -> str:
        p = f"{self.base_url}/{db}"
        if doc_id:
            p += f"/{doc_id}"
        if sub:
            p += f"/{sub}"
        return p

    def get(self, db: str, doc_id: Optional[str] = None, **params) -> Any:
        r = requests.get(self._path(db, doc_id), auth=self.auth, params=params, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def put(self, db: str, doc_id: str, doc: dict) -> dict:
        """Create or update a document; returns the stored document (with
        `_id`/`_rev`) so callers can hand back what was actually persisted."""
        payload = dict(doc)
        payload["_id"] = doc_id
        r = requests.put(self._path(db, doc_id), auth=self.auth, json=payload, timeout=10)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"CouchDB write failed for {db}/{doc_id}: {r.status_code} {r.text}")
        out = dict(payload)
        out["_rev"] = r.json().get("rev", out.get("_rev", "1-r1"))
        return out

    def delete(self, db: str, doc_id: str) -> None:
        doc = self.get(db, doc_id)
        if not doc:
            return
        r = requests.delete(self._path(db, doc_id), params={"rev": doc["_rev"]},
                             auth=self.auth, timeout=10)
        if r.status_code not in (200, 202):
            raise RuntimeError(f"CouchDB delete failed for {db}/{doc_id}: {r.status_code} {r.text}")

    def mango(self, db: str, selector: dict, sort: Optional[list] = None,
              limit: int = 1000) -> list[dict]:
        payload: dict[str, Any] = {"selector": selector, "limit": limit}
        if sort:
            payload["sort"] = sort
        r = requests.post(self._path(db, sub="_find"), auth=self.auth, json=payload, timeout=15)
        if r.status_code != 200:
            # fall back to a full scan (e.g. no index yet on a fresh db)
            r2 = requests.get(self._path(db), auth=self.auth,
                               params={"include_docs": True, "limit": limit}, timeout=15)
            if r2.status_code != 200:
                return []
            return [row["doc"] for row in r2.json().get("rows", [])]
        return r.json().get("docs", [])

    def window(self, db: str, field: str, frm: Optional[str], to: Optional[str],
               order: str = "asc", limit: int = 2000) -> list[dict]:
        """Documents whose `field` (an event-time) falls within [frm, to],
        inclusive, sorted by that field. Bounds are normalized ISO strings."""
        rng: dict = {}
        if frm:
            rng["$gte"] = frm
        if to:
            rng["$lte"] = to
        selector: dict = {field: rng} if rng else {}
        docs = self.mango(db, selector, sort=[{field: order}], limit=limit)
        # belt-and-braces client-side filter, in case the fallback full-scan
        # path above was taken (no Mango index yet)
        def _in_range(d: dict) -> bool:
            v = d.get(field)
            if not v:
                return False
            if frm and v < frm:
                return False
            if to and v > to:
                return False
            return True
        docs = [d for d in docs if _in_range(d)]
        docs.sort(key=lambda d: d.get(field, ""), reverse=(order == "desc"))
        return docs

    def put_attachment(self, db: str, doc_id: str, name: str, data: bytes,
                        content_type: str) -> dict:
        doc = self.get(db, doc_id)
        if not doc:
            raise RuntimeError(f"{db}/{doc_id} not found")
        r = requests.put(self._path(db, doc_id, name), auth=self.auth,
                          params={"rev": doc["_rev"]}, data=data,
                          headers={"Content-Type": content_type}, timeout=20)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"attachment write failed: {r.status_code} {r.text}")
        return r.json()

    def get_attachment(self, db: str, doc_id: str, name: str) -> Optional[tuple[bytes, str]]:
        r = requests.get(self._path(db, doc_id, name), auth=self.auth, timeout=20)
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "application/octet-stream")
        content = r.content if hasattr(r, "content") else r._payload
        return content, ctype

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/_up", auth=self.auth, timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    def create_db(self, db: str) -> None:
        r = requests.put(self._path(db), auth=self.auth, timeout=10)
        if r.status_code not in (201, 202, 409, 412):
            raise RuntimeError(f"failed to create db {db!r}: {r.status_code} {r.text}")

    def create_index(self, db: str, fields: list[str], name: str) -> None:
        r = requests.post(self._path(db, sub="_index"), auth=self.auth,
                           json={"index": {"fields": fields}, "name": name}, timeout=10)
        # 200 = created/exists; anything else is non-fatal (older CouchDB, etc.)
