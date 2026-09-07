"""In-memory fake CouchDB that emulates the HTTP calls couch.py makes.

Monkeypatches `couch.requests` (every CouchDB call in this app funnels
through couch.py's `Couch` class) with a fake that answers GET / PUT /
POST(_find) / DELETE — including attachments — the way CouchDB would, so
tests exercise the real endpoint logic without a running CouchDB.
"""
import copy
import json
import re


class FakeResponse:
    def __init__(self, status_code, payload=None, content=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self.text = "" if payload is None else json.dumps(payload)
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self._payload

    @property
    def content(self):
        if self._content is not None:
            return self._content
        raise AttributeError("content")  # forces couch.py's hasattr() fallback to _payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code}: {self.text}")


class FakeCouch:
    """A tiny stand-in for CouchDB that stores docs (+ attachments) per db."""

    DBS = ["items", "item_links", "intake", "holidays", "symptom_items", "health",
           "bathroom_items", "bathroom_events", "notes", "findings", "users", "passkeys"]

    def __init__(self):
        self.dbs: dict[str, dict] = {d: {} for d in self.DBS}
        self.attachments: dict[tuple, tuple] = {}  # (db, doc, name) -> (bytes, content_type)

    @staticmethod
    def _parse(url, params=None):
        s = re.sub(r"^https?://[^/]+/", "", str(url)).split("?", 1)[0]
        if s == "_up":
            return {"special": "up"}
        parts = [p for p in s.split("/") if p != ""]
        is_find = "_find" in parts
        is_index = "_index" in parts
        db = parts[0] if parts else None
        rest = [p for p in parts[1:] if p not in ("_find", "_index")]
        doc = rest[0] if rest else None
        attachment = rest[1] if len(rest) > 1 else None
        return {"db": db, "doc": doc, "attachment": attachment,
                "is_find": is_find, "is_index": is_index, "params": params or {}}

    # ── HTTP verbs ──────────────────────────────────────────────────────
    def get(self, url, auth=None, params=None, timeout=None, **kw):
        p = self._parse(url, params)
        if p.get("special") == "up":
            return FakeResponse(200, {"status": "ok", "seeds": {}})
        db, doc, attachment = p["db"], p["doc"], p["attachment"]
        if doc and attachment:
            key = (db, doc, attachment)
            if key not in self.attachments:
                return FakeResponse(404, {"error": "not_found"})
            data, ctype = self.attachments[key]
            return FakeResponse(200, content=data, headers={"content-type": ctype})
        store = self.dbs.setdefault(db, {})
        if doc:
            if doc in store:
                return FakeResponse(200, copy.deepcopy(store[doc]))
            return FakeResponse(404, {"error": "not_found"})
        rows = [{"id": k, "doc": copy.deepcopy(v), "value": {"rev": v.get("_rev", "1-x")}}
                for k, v in store.items()]
        return FakeResponse(200, {"rows": rows})

    def put(self, url, auth=None, json=None, data=None, headers=None, params=None, timeout=None, **kw):
        p = self._parse(url, params)
        db, doc, attachment = p["db"], p["doc"], p["attachment"]
        if not doc:
            return FakeResponse(500, {"error": "need doc id"})
        if attachment is not None and json is None:
            # attachment upload: raw bytes via `data=`
            ctype = (headers or {}).get("Content-Type", "application/octet-stream")
            self.attachments[(db, doc, attachment)] = (data, ctype)
            return FakeResponse(200, {"ok": True, "id": doc, "rev": "att-1"})
        store = self.dbs.setdefault(db, {})
        body = dict(json or {})
        body.setdefault("_rev", "1-r1")
        # emulate a rev bump on every write, like real CouchDB
        if doc in store:
            rev_n = int(store[doc].get("_rev", "1-r1").split("-")[0]) + 1
            body["_rev"] = f"{rev_n}-r{rev_n}"
        store[doc] = body
        return FakeResponse(201, {"ok": True, "id": doc, "rev": body["_rev"]})

    def post(self, url, auth=None, json=None, headers=None, timeout=None, **kw):
        p = self._parse(url, (json or {}).get("params"))
        db = p["db"]
        if p.get("is_index"):
            return FakeResponse(200, {"result": "created"})
        if p.get("is_find"):
            selector = (json or {}).get("selector", {})
            sort = (json or {}).get("sort")
            limit = (json or {}).get("limit", 1000)
            docs = [copy.deepcopy(d) for d in self.dbs.get(db, {}).values()]
            docs = self._apply_selector(docs, selector)
            if sort:
                field = list(sort[0].keys())[0]
                reverse = sort[0].get(field) == "desc"
                docs.sort(key=lambda d: (d.get(field) is None, d.get(field)), reverse=reverse)
            return FakeResponse(200, {"docs": docs[:limit]})
        return FakeResponse(200, {"ok": True})

    def delete(self, url, auth=None, params=None, timeout=None, **kw):
        p = self._parse(url, params)
        db, doc = p["db"], p["doc"]
        store = self.dbs.setdefault(db, {})
        if doc in store:
            del store[doc]
            return FakeResponse(200, {"ok": True})
        return FakeResponse(404, {"error": "not_found"})

    # ── Mango selector (only the operators this app uses) ────────────────
    def _apply_selector(self, docs, sel):
        return [d for d in docs if self._match(d, sel)]

    def _match(self, d, sel):
        for field, cond in sel.items():
            val = d.get(field)
            if isinstance(cond, dict):
                for op, target in cond.items():
                    if not self._op(val, op, target):
                        return False
            else:
                if val != cond:
                    return False
        return True

    def _op(self, val, op, target):
        if op == "$gte":
            return val is not None and val >= target
        if op == "$lte":
            return val is not None and val <= target
        if op == "$gt":
            return val is not None and val > target
        if op == "$lt":
            return val is not None and val < target
        if op == "$regex":
            return re.match(target, val or "") is not None
        return False


def install_fake(module):
    """Replace the `requests` used by a module (couch.py) with our fake."""
    fake = FakeCouch()
    module.requests = fake
    return fake
