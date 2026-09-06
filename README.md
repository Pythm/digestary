# Digestary

**Track what you eat and how you feel — and let an AI help you see the connection.**

Digestary is an open-source, **self-hosted diet & symptoms journal for anyone
tracking digestive or stomach issues and looking for patterns between food
and symptoms.** It works for **all ages** and **all bodies** — no medical
specialty, no personal details baked in. You run it yourself on any small
machine (a Proxmox LXC, a Raspberry Pi, a home server), and an **LLM is
optional** — the whole journal works with no artificial intelligence at all.

> **Important:** Digestary is a personal tracking and reflection tool. It is
> **not medical advice and does not diagnose anything.** Bring the patterns
> you find to a doctor.

---

## Why it exists

When you have a stomach- or digestive-sensitive condition, the hardest part
is *seeing the connection* between what you ate and how you felt, especially
over days and weeks. People often can't remember the exact timing, or a
symptom is only logged hours after it happened — which makes it look like
the last meal caused it, when it didn't.

Digestary is built to make that connection visible:

- **Every entry carries one editable *when it happened* time.** You set (and
  can change) **when the thing actually happened** — it may be the past
  (logging after the fact) **or the future** (pre-logging a packed lunch).
  The app and the AI reason on that *happened* time, so a bathroom event you
  logged an hour later is never mistaken for something the food you ate just
  before caused. There is no separate "when it was entered" time anywhere.
- **A true chronological timeline** interleaves everything (meals, daily
  routine, bathroom events, holidays) in the order it really happened.
- **You can ask anything.** Type a free-form question — "which foods show up
  before my stomach pain?" — and get the relevant rows back, or connect an
  MCP client for a written answer over the exact same data. A doctor (or you)
  can ask *specific* questions and get the data processed in that exact way,
  far beyond a generic summary.

---

## Features

| Section | What it captures |
|---|---|
| **Diet** | A **text box** — type a food (in your own language) and the list of foods you've logged before **narrows to the matches**. Tick a match to log it, or type a new one. An item with **sub-options** (e.g. bread with butter / jam / cheese) opens a **popup** offering them — the same type-or-select logic, recursively. A meal also records **who prepared it** (home-prepared / prepared elsewhere, with an optional place name — a restaurant, school, caterer) and a single **editable "when consumed" time** (now / past / future). |
| **Calendar — holidays** | Add a **holiday** as a date range (from → to) with a location; it shows as a **band on the timeline**, so "was on holiday" can be correlated with how you felt. A one-time restaurant is simply the food log's "prepared elsewhere" field with a place name — no separate table. |
| **Daily routine** | Temperature (°C, fever-colored at the configured threshold), energy (0–4), sleep hours, a **type-ahead symptom list** in your own words, a **painted body map** (front/back, tap a region to mark mild/moderate/severe) plus an optional overall 0–10 pain score, and free-text notes. Saved once per day. |
| **Bathroom events** | Log an event by its **kind** — a type-ahead over your own previously-logged words, not a fixed list — with an editable **when it happened** time, a note, and an **optional photo**. |
| **Notes** | Free-text "other information" for any day. |
| **Timeline** | The last 14 days, interleaved and sorted **by the time each event actually happened**, holidays shown as a band. |
| **Ask** | A free-form question box that always returns the relevant logged rows for a window — works with zero AI. Connect an MCP client for a written answer over the same data. |
| **Findings** | Saved correlations and Q&A, so a later session (or a different device, or a doctor) can read the context without re-analysing. |
| **AI (MCP) — always on, LLM optional** | A small **MCP server ships with the app** as a first-class part of the stack. Connect any MCP client (Claude Code, Hermes, Continue, Cline, …) or don't — the journal works fully offline. |
| **Login / guest access** | Run it locally with no login, or turn on real username/password accounts plus an optional **guest code** that lets someone (on a phone at a restaurant, on holiday) add entries without being able to delete anything or manage the catalog. |

---

## Quick start (Docker)

```bash
git clone <REPO_URL> digestary
cd digestary
cp .env.example .env         # edit .env: set a strong COUCHDB_PASSWORD (and,
                              # if you turn on AUTH_MODE=public, SESSION_SECRET
                              # + OWNER_USERNAME/OWNER_PASSWORD)
docker compose up -d --build
```

Then open:

```
http://<your-server-ip>:8080
```

Use it on a phone (the UI is mobile-first), log a meal, save today's
routine, log a bathroom event, and check the 14-day timeline.

**Back up:** `scripts/backup.sh` (creates a dated tarball of the data volume).

> The app is small and CPU-light. The AI/MCP layer adds almost nothing to the
> container unless you actually call it.

---

## Architecture

```
   ┌──────────────┐        HTTP/JSON         ┌──────────────────┐
   │  Browser UI    │  ─────────────────────▶  │  app   (FastAPI)  │
   │  index.html + │  ◀──────────────────────  │  static + REST    │
   │  style + app   │                           └────────┬─────────┘
   └──────────────┘                                     │
                                                        │ reads / writes
                                                        ▼
                                              ┌─────────────────────┐
                                              │   couchdb (3.x)      │
                                              │   11 databases:       │
                                              │  items, item_links,   │
                                              │  intake, holidays,    │
                                              │  symptom_items,       │
                                              │  health,               │
                                              │  bathroom_items,      │
                                              │  bathroom_events,     │
                                              │  notes, findings,      │
                                              │  users                │
                                              └─────────────────────┘
                                                        ▲
                                                        │ MCP: reads everything,
                                                        │ writes gated by MCP_SECRET
                                              ┌────────┴──────────┐
                                              │  mcp   (MCP server)│
                                              │   (always part of   │
                                              │   the stack — the   │
                                              │   LLM that talks to  │
                                              │   it is optional)    │
                                              └─────────────────────┘
                                                        ▲
                                                        │
                                             any MCP client / LLM
                           (Claude Code, Hermes, Continue, Cline,
                           an OpenAI-compatible API, … — or nothing)
```

- **`app`** — a small FastAPI service that serves the static frontend and
  exposes a JSON REST API. On first start it **auto-creates the 11 databases
  and leaves them empty** — **no food catalog is seeded**; the item list
  fills in as the user logs.
- **`couchdb`** — an [Apache CouchDB 3.x](https://couchdb.apache.org/)
  document database. Open-source, Docker-friendly, and shaped like the data
  actually is (JSON documents). The data volume is a single folder, which
  makes backing up trivial.
- **`mcp`** — an **MCP server** that is **always part of the stack**. It
  reads CouchDB directly and exposes a handful of write tools, every one
  gated by `MCP_SECRET`. The LLM that talks to it is **optional**: you can
  run the whole app without ever connecting a model.

> **Why CouchDB, and not a "Firestore clone"?** Google Firestore is not open
> source, so there is no true open-source clone of it. CouchDB (Apache-2.0)
> is the closest genuinely open-source, Docker-native document store: one
> small container, JSON documents, a one-folder backup.

---

## The single "when it happened" time (the key idea)

Every entry stores **one time — when the thing actually happened** (ate,
felt, pee'd/poo'd): `consumed_at` for food, `event_at` for everything else.
It **defaults to now** but you can move it to an **earlier** time (logging
after the fact) or a **future** one (e.g. pre-logging a packed lunch). There
is **no separate "when it was entered" timestamp** anywhere.

**Why this matters:** imagine you get a stomach ache, log it an hour later
after a meal, and a bathroom event two hours after that. If the app only
stored "the day" or your *recording* time, it could look like the meal
caused the event. Because the *happened* time is the real timeline, the app
and the AI see the **true order of events**, and correlation is honest.

The **Timeline**, **Ask**, and every MCP read tool order and window on this
one time.

---

## Data model (CouchDB databases)

| Database | Contents |
|---|---|
| `items` | Every food / option, all equal (one document per item), in your own words. **Empty on first start**; `is_parent` is never stored — it's computed from `item_links` so the two can never drift. |
| `item_links` | A link from a sub-item to a parent item (e.g. *butter* → *bread*). Recursive — a child can itself be a parent. UI suggestion only. |
| `intake` | **One document per selected food item** (a meal is a group of lines sharing an `intake_id`), each with a single `consumed_at`, a `where` (`home_prepared` / `out_prepared`) + optional `where_name` (the restaurant/school/caterer's name), and an `author`. |
| `holidays` | One document per trip: `start`, `stop` (a date range), `location`, optional `name`. Shown as a band on the timeline. |
| `symptom_items` | Every symptom you've logged, flat, in your own words. Empty on first start — no fixed vocabulary. |
| `health` | One document per date (upserted): temperature, energy, sleep, `symptoms` (references into `symptom_items`), `pain_map` (a painted-region body map), optional `pain_scale`, notes, author. |
| `bathroom_items` | Every bathroom-event kind you've logged, flat, in your own words. Empty on first start. |
| `bathroom_events` | One document per event: `event_at`, `kind` (references `bathroom_items`), notes, an optional photo attachment, author. |
| `notes` | Free-text "other information", each with a single `event_at`. |
| `findings` | AI correlations and saved Q&A. The only place an LLM is allowed to write (besides an item's `emoji`). |
| `users` | Login accounts (`AUTH_MODE=public` only) — username, a salted/hashed password, role. |

---

## Connecting an LLM (optional)

The **MCP server ships with the app and is always running** as part of the
stack. You only need *a model* if you want AI answers — nothing in the app
is broken or degraded without one.

The MCP server (`mcp/mcp_server.py`) exposes these tools:

| Tool | Direction | Description |
|---|---|---|
| `list_items` / `list_item_links` | read | The food catalog, flat and equal, plus sub-item suggestions |
| `list_symptom_items` / `list_bathroom_items` | read | Your own symptom words / bathroom-event kinds |
| `get_intake(from_date, to_date)` | read | Logged meals in a window (on `consumed_at`) |
| `get_holidays(from_date, to_date)` | read | Holidays overlapping a window |
| `get_health(from_date, to_date)` | read | Daily routines in a window |
| `get_bathroom_events(from_date, to_date)` | read | Bathroom events in a window |
| `get_bathroom_event_photo(event_id)` | read | The optional photo on an event, base64-encoded, for a vision-capable model |
| `get_notes(from_date, to_date)` | read | Notes in a window |
| `get_summary(from_date, to_date)` | read | Aggregated stats — a good first call |
| `ask(question, from_date, to_date)` | read | The relevant rows for a window, formatted for a model to reason over |
| `add_finding(...)` | **write** | Save a correlation or Q&A — gated by `MCP_SECRET` |
| `update_item(item_id, emoji)` | **write** | Set an item's emoji — **manual**, see below |
| `add_item` / `add_symptom_item` / `add_bathroom_item` | **write** | Add one catalog entry, in the language set as `LANGUAGE` — **manual**, see below |
| `list_findings` | read | All saved findings / Q&A, newest first |

The model has **read access to everything** but **write access only to
`findings` and item helpers above**. It can never alter your raw diet /
routine / bathroom data. Without `MCP_SECRET` configured, the server can't
write at all; over the HTTP transport (port 8090), **every** request —
reads included — must carry `X-MCP-Secret`.

### Filling in food emoji, or prefilling a new user's lists (manual, on request)

The MCP server does **not** scan the database for missing emoji or empty
catalog tables on its own — every connection would pay that cost even for an
unrelated question. Instead, these are things you **ask** a connected agent
to do:

> "Please fill in emoji for any food items that don't have one yet."
> "This is a new journal — prefill `items`, `symptom_items`, and
> `bathroom_items` with the most common options in Norwegian."

The agent does this itself with `list_items` + `update_item` / `add_item` /
`add_symptom_item` / `add_bathroom_item` — no separate job or schedule
needed. It's additive and idempotent: it never renames or deletes anything,
and re-running it only fills in what's still missing. `LANGUAGE` in `.env`
tells the agent which language to use; the app itself is otherwise
language-agnostic — whatever you type is exactly what's stored.

### Bringing the data to a doctor's visit

There's no export file to generate — bring your own laptop (already logged
in), connect it to the MCP server the same way you would at home, and ask
the questions the doctor wants answered **live, against the real data**:

> "In the last 3 weeks, what did they eat in the 4 hours before each
> stomach-pain day?"
> "Compare the two weeks on holiday against the two weeks before it."

`Ask`/`ask` always shows the underlying rows alongside any answer, so both
of you can verify it on the spot.

### Connecting an MCP client

```bash
# Claude Code
claude --mcp-config mcp.json
```
```json
// mcp.json
{
  "mcpServers": {
    "digestary": {
      "command": "python",
      "args": ["/opt/digestary/mcp/mcp_server.py"],
      "env": {
        "COUCHDB_URL": "http://127.0.0.1:5984",
        "COUCHDB_USER": "admin",
        "COUCHDB_PASSWORD": "your-couchdb-password",
        "MCP_SECRET": "your-mcp-write-secret"
      }
    }
  }
}
```

Or point at the always-on HTTP endpoint (`http://<host>:8090`, requires the
`X-MCP-Secret` header on every request) from any MCP-over-HTTP client.

---

## Login and guest access

- **`AUTH_MODE=local` (default):** no login — open on your LAN. Good for a
  personal home device; anyone on the network can use it.
- **`AUTH_MODE=public`:** a real account (username + salted/hashed password,
  a signed session cookie, rate-limited login attempts) is required for
  owner access. Set `OWNER_USERNAME`/`OWNER_PASSWORD` in `.env` to bootstrap
  the first account on first boot; add more from the UI (or
  `POST /api/auth/users` while logged in) for other household members.
  `SESSION_SECRET` (generate with `openssl rand -hex 32`) is required in
  this mode.
- **`GUEST_TOKEN`** (either mode): a shared code that lets someone *add*
  diet / routine / bathroom / notes entries from a device with no account —
  a phone at a restaurant, a tablet on holiday — but **never delete anything
  or manage the food catalog**. Every entry still records an `author`, so
  different people's logs stay distinguishable.
- **Not yet built:** a passkey (WebAuthn/FIDO2) or TOTP second factor. The
  password/session layer above is real, not a placeholder, so this can be
  added on top of it later without a rewrite — it's a deliberate v2, not
  because the current login is a stub.

---

## Deploying on a Proxmox LXC

1. **Create an LXC container** with at least 1 vCPU, 1–2 GB RAM, 5 GB disk.
2. **Install Docker inside the LXC:**
   ```bash
    apt-get update
    apt-get install -y docker.io
    systemctl enable --now docker
    usermod -aG docker $USER     # re-login after this
   ```
3. **Get the app:**
   ```bash
  git clone <REPO_URL> /opt/digestary
  cd /opt/digestary
  cp .env.example .env       # set COUCHDB_PASSWORD, MCP_SECRET, and
                              # (if AUTH_MODE=public) SESSION_SECRET +
                              # OWNER_USERNAME/OWNER_PASSWORD
   ```
4. **Start it:**
   ```bash
  docker compose up -d --build
   ```
5. **Open the UI:** `http://<lxc-ip>:8080`
6. **Back up regularly:** `scripts/backup.sh`

### Multiple people = one stack per person

Each person gets their **own complete stack** — own CouchDB, own `app`, own
`mcp` — rather than one shared CouchDB with per-person tables. Clone the
repo again into its own directory (e.g. `/opt/digestary-anna`), give it its
own `.env` (its own `COUCHDB_PASSWORD`, `MCP_SECRET`, `LANGUAGE`, and host
ports: `APP_HOST_PORT`, `MCP_HOST_PORT`, `COUCHDB_HOST_PORT`), and run
`docker compose up -d --build` in that directory. Backup is then one folder
per person, and there's no schema change needed to add a third person.

---

## Backup

All data lives in the named Docker volume `digestary-data` (which maps to
`/opt/couchdb/data` in the container). Backing up is just copying that
folder:

```bash
# scripts/backup.sh
docker run --rm \
   -v digestary-data:/data \
   -v "$(pwd)/backups:/backup" \
  alpine sh -c 'tar czf /backup/couchdb-$(date +%Y%m%d-H%M%S).tar.gz -C /data .'
```

To restore, stop the stack, decompress the tarball back onto the same volume
path, and restart.

---

## Security notes

- **`local` mode (default):** open on your LAN — anyone on the network can
  reach `http://<ip>:8080`. That's fine for a personal home device.
- **`public` mode:** real accounts (PBKDF2-HMAC-SHA256, per-user salt, a
  signed session cookie, rate-limited login attempts) for owner access,
  plus the optional add-only guest code.
- **Do not expose the UI to the public internet without a reverse proxy**
  (Caddy/Nginx) that terminates TLS, unless you deliberately want public
  guest logging.
- The **CouchDB admin port is never published to the host** beyond the
  optional direct-access port for backups; only the app's HTTP port (8080)
  and the MCP port (8090) are meant to be LAN-reachable.
- **The MCP HTTP endpoint (8090) requires `X-MCP-Secret` on every request**
  — reads included — regardless of network. Over stdio (an agent launching
  it as a local subprocess) only the write tools check the secret, since the
  process itself is already local-trusted.
- An LLM **cannot** modify your raw data — only append to `findings`, and
  fill in an item's `emoji` / a missing catalog entry, and only when asked.

---

## License

Apache-2.0. See `LICENSE`.

---

## Contributing

1. Open an issue describing the feature or bug.
2. Fork, branch, and submit a PR.
3. Keep the UI mobile-first and usable by people of all ages; the dark theme
   is a **default, not a requirement** — color is a free design choice.
4. Run the test suite before submitting: `python -m pytest app/tests -q`.
