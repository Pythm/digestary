// Digestary — vanilla JS frontend. No build step, no framework.
"use strict";

// ── state ────────────────────────────────────────────────────────────────
const state = {
  config: null,
  principal: null,               // { username, role }
  items: [],
  itemLinks: [],
  symptomItems: [],
  bathroomItems: [],
  mealDraft: [],                 // [{food_id, name, emoji}]
  selectedSymptoms: new Map(),   // id -> name
  selectedBathroomKind: null,    // {_id, name}
  painMap: {},                   // region -> "mild"|"moderate"|"severe"
  activeTab: "today",
  subOptionCtx: null,            // {parentItem, ticked:Map(id->doc), alreadyAddedParent}
};

const SEVERITY_CYCLE = [null, "mild", "moderate", "severe"];
const ENERGY_LABELS = ["Very low", "Low", "Normal", "Good", "Great"];
const TYPE_ICON = { meal: "🍽", routine: "🩺", bathroom: "🚻", note: "📝" };

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ── toast ────────────────────────────────────────────────────────────────
function toast(msg, kind = "") {
  const host = $("#toast-host");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ── time helpers ─────────────────────────────────────────────────────────
function toIso(localValue) {
  if (!localValue) return null;
  return new Date(localValue).toISOString();
}
function nowLocalInput() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}
function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}
function fmtTime(iso) {
  if (!iso) return "?";
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function fmtDateHeading(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}
function dateKey(iso) { return String(iso || "").slice(0, 10); }
function todayDateKey() { return new Date().toISOString().slice(0, 10); }
function daysAgoIso(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString();
}

// ── API ──────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "include", ...opts });
  if (res.status === 401) {
    showLoginGate();
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    toast(detail, "error");
    throw new Error(detail);
  }
  const ctype = res.headers.get("content-type") || "";
  return ctype.includes("application/json") ? res.json() : res.text();
}
function apiJson(path, method, body) {
  return api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
async function loadPhotoBlobUrl(path) {
  try {
    const res = await fetch(path, { credentials: "include" });
    if (!res.ok) return null;
    return URL.createObjectURL(await res.blob());
  } catch (_) {
    return null;
  }
}

// ── auth ─────────────────────────────────────────────────────────────────
function showLoginGate() {
  $("#app").classList.add("hidden");
  $("#login-gate").classList.remove("hidden");
}
function showApp() {
  $("#login-gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
}
function updateRoleBadge() {
  const badge = $("#role-badge");
  if (!state.principal || state.config.auth_mode !== "public") {
    badge.classList.add("hidden");
    $("#logout-btn").classList.add("hidden");
    return;
  }
  badge.classList.remove("hidden");
  badge.textContent = state.principal.username;
  badge.className = "badge owner";
  $("#logout-btn").classList.remove("hidden");
}

async function tryAuthAndEnter() {
  state.config = await api("/api/config");
  if (state.config.auth_mode !== "public") {
    state.principal = { username: "owner", role: "owner" };
    showApp();
    updateRoleBadge();
    await bootApp();
    return;
  }
  try {
    state.principal = await api("/api/auth/me");
    showApp();
    updateRoleBadge();
    await bootApp();
  } catch (_) {
    showLoginGate();
  }
}

// ── WebAuthn (passkeys) — base64url <-> ArrayBuffer plumbing ────────────
function b64urlToBuf(s) {
  const bin = atob((s + "=".repeat((4 - (s.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function bufToB64url(buf) {
  let bin = "";
  for (const b of new Uint8Array(buf)) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function decodeCreationOptions(opts) {
  return {
    ...opts,
    challenge: b64urlToBuf(opts.challenge),
    user: { ...opts.user, id: b64urlToBuf(opts.user.id) },
    excludeCredentials: (opts.excludeCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
}
function decodeRequestOptions(opts) {
  return {
    ...opts,
    challenge: b64urlToBuf(opts.challenge),
    allowCredentials: (opts.allowCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
}
function credentialToJson(cred) {
  const isRegistration = !!cred.response.attestationObject;
  const response = isRegistration ? {
    clientDataJSON: bufToB64url(cred.response.clientDataJSON),
    attestationObject: bufToB64url(cred.response.attestationObject),
  } : {
    clientDataJSON: bufToB64url(cred.response.clientDataJSON),
    authenticatorData: bufToB64url(cred.response.authenticatorData),
    signature: bufToB64url(cred.response.signature),
    userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null,
  };
  return { id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type, response };
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  $("#login-error").textContent = "";
  if (!username || !password) return;
  try {
    const res = await apiJson("/api/auth/login", "POST", { username, password });
    state.principal = res.mfa_required ? await completePasskeyLogin(res) : res;
    showApp();
    updateRoleBadge();
    await bootApp();
  } catch (err) {
    $("#login-error").textContent = err.passkeyStep
      ? "Passkey verification failed or was cancelled."
      : "Invalid username/password, or too many attempts.";
  }
}
async function completePasskeyLogin(mfa) {
  $("#login-sub").textContent = "Confirm with your passkey…";
  try {
    if (!window.PublicKeyCredential) throw new Error("This browser doesn't support passkeys.");
    const assertion = await navigator.credentials.get({ publicKey: decodeRequestOptions(mfa.options) });
    return await apiJson("/api/auth/passkeys/login/verify", "POST", {
      ticket: mfa.ticket, credential: credentialToJson(assertion),
    });
  } catch (e) {
    e.passkeyStep = true;
    throw e;
  } finally {
    $("#login-sub").textContent = "A private diet & symptoms journal.";
  }
}
async function handleLogout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  location.reload();
}

// ── theme ────────────────────────────────────────────────────────────────
function applyStoredTheme() {
  try {
    const t = localStorage.getItem("digestary_theme");
    if (t) document.documentElement.dataset.theme = t;
  } catch (_) {}
}
function toggleTheme() {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("digestary_theme", next); } catch (_) {}
}

// ── tab navigation ───────────────────────────────────────────────────────
const TAB_LABELS = { today: "Today", timeline: "Timeline", insights: "Insights", settings: "Settings" };
function goToTab(target) {
  state.activeTab = target;
  $$(".page").forEach((el) => el.classList.toggle("active", el.dataset.page === target));
  $$(".tab-item").forEach((btn) => {
    const on = btn.dataset.target === target;
    btn.classList.toggle("active", on);
    if (on) btn.setAttribute("aria-current", "page"); else btn.removeAttribute("aria-current");
  });
  $("#page-heading").textContent = TAB_LABELS[target] || "Digestary";
  window.scrollTo(0, 0);
  if (target === "timeline") { loadTimeline(); loadTodayHealth(); }
  if (target === "insights") loadFindings();
}

// ── generic type-ahead rendering ─────────────────────────────────────────
function filterByName(list, query) {
  const q = query.trim().toLowerCase();
  if (!q) return list.slice(0, 40);
  return list.filter((i) => i.name.toLowerCase().includes(q)).slice(0, 40);
}
function exactMatch(list, query) {
  const q = query.trim().toLowerCase();
  return list.find((i) => i.name.toLowerCase() === q);
}
function renderChips(listEl, items, { onTap, query, onAddNew, emoji, isSelected }) {
  listEl.innerHTML = "";
  for (const it of items) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (isSelected && isSelected(it) ? " selected" : "");
    chip.innerHTML = (emoji && it.emoji ? `<span class="emoji">${escapeHtml(it.emoji)}</span> ` : "") + escapeHtml(it.name);
    chip.addEventListener("click", () => onTap(it));
    listEl.appendChild(chip);
  }
  const q = (query || "").trim();
  if (q && onAddNew && !exactMatch(items.__all || items, q)) {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "chip new";
    add.textContent = `+ Add "${q}"`;
    add.addEventListener("click", () => onAddNew(q));
    listEl.appendChild(add);
  }
}

// ── food (diet) ──────────────────────────────────────────────────────────
async function loadItems() {
  state.items = await api("/api/items");
  state.itemLinks = await api("/api/item-links");
  renderFoodTypeahead();
}
function childrenOf(parentId) {
  return state.itemLinks.filter((l) => l.parent === parentId)
    .map((l) => state.items.find((i) => i._id === l.child)).filter(Boolean);
}
function renderFoodTypeahead() {
  const q = $("#food-input").value;
  const matches = filterByName(state.items, q);
  matches.__all = state.items;
  renderChips($("#food-list"), matches, {
    query: q, emoji: true,
    isSelected: (it) => state.mealDraft.some((d) => d.food_id === it._id),
    onTap: (it) => selectFoodItem(it, false),
    onAddNew: (name) => openNewItemModal(name),
  });
}
function addToDraft(item) {
  if (state.mealDraft.some((d) => d.food_id === item._id)) return;
  state.mealDraft.push({ food_id: item._id, name: item.name, emoji: item.emoji });
  renderMealDraft();
}
function renderMealDraft() {
  $("#meal-draft-wrap").classList.toggle("hidden", state.mealDraft.length === 0);
  const ul = $("#meal-draft-list");
  ul.innerHTML = "";
  for (const d of state.mealDraft) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${d.emoji ? escapeHtml(d.emoji) + " " : ""}${escapeHtml(d.name)}</span>`;
    const rm = document.createElement("button");
    rm.type = "button"; rm.className = "small ghost remove-btn"; rm.textContent = "Remove";
    rm.addEventListener("click", () => {
      state.mealDraft = state.mealDraft.filter((x) => x.food_id !== d.food_id);
      renderMealDraft();
      renderFoodTypeahead();
    });
    li.appendChild(rm);
    ul.appendChild(li);
  }
  renderFoodTypeahead();
}
function selectFoodItem(item, alreadyPlannedForSubpopup) {
  addToDraft(item);
  $("#food-input").value = "";
  renderFoodTypeahead();
  const kids = childrenOf(item._id);
  if (kids.length && !alreadyPlannedForSubpopup) {
    openSuboptionModal(item, { alreadyAddedParent: true });
  }
}

function clearIntakeForm() {
  state.mealDraft = [];
  renderMealDraft();
  $("#intake-notes").value = "";
  $("#intake-where-name").value = "";
  $("#intake-where").value = "home_prepared";
  $("#where-name-field").classList.add("hidden");
  $("#intake-consumed-at").value = nowLocalInput();
}
async function saveIntake() {
  if (!state.mealDraft.length) { toast("Add at least one food first.", "error"); return; }
  try {
    await apiJson("/api/intake", "POST", {
      food_ids: state.mealDraft.map((d) => d.food_id),
      consumed_at: toIso($("#intake-consumed-at").value),
      where: $("#intake-where").value,
      where_name: $("#intake-where-name").value.trim() || null,
      notes: $("#intake-notes").value.trim(),
    });
    toast("Meal saved.", "success");
    clearIntakeForm();
    await Promise.all([loadIntakeLog(), loadTimeline()]);
  } catch (err) {
    toast("Could not save meal: " + err.message, "error");
  }
}

async function loadIntakeLog() {
  const today = todayDateKey();
  const rows = await api(`/api/intake?frm=${today}&to=${today}`);
  const groups = new Map();
  for (const line of rows) {
    if (!groups.has(line.intake_id)) groups.set(line.intake_id, []);
    groups.get(line.intake_id).push(line);
  }
  const ul = $("#intake-log-list");
  ul.innerHTML = "";
  const sorted = Array.from(groups.entries()).sort((a, b) => (b[1][0].consumed_at < a[1][0].consumed_at ? -1 : 1));
  $("#intake-empty").classList.toggle("hidden", sorted.length > 0);
  for (const [intakeId, lines] of sorted) {
    const names = lines.map((l) => itemLabel(l.food_id)).join(", ");
    const where = lines[0].where === "out_prepared" ? "Out-prepared" : "Home-prepared";
    const li = document.createElement("li");
    li.innerHTML = `<div class="item-main"><strong>${escapeHtml(names)}</strong>
      <span class="meta">${fmtTime(lines[0].consumed_at)} · ${where}${lines[0].where_name ? " @ " + escapeHtml(lines[0].where_name) : ""}</span></div>`;
    const actions = document.createElement("div");
    actions.className = "actions";
    if (state.principal.role === "owner") {
      const del = document.createElement("button");
      del.type = "button"; del.className = "small ghost"; del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm("Delete this whole meal?")) return;
        await api(`/api/intake/group/${intakeId}`, { method: "DELETE" });
        toast("Meal deleted.", "success");
        await Promise.all([loadIntakeLog(), loadTimeline()]);
      });
      actions.appendChild(del);
    }
    li.appendChild(actions);
    ul.appendChild(li);
  }
}
function itemLabel(foodId) {
  const it = state.items.find((i) => i._id === foodId);
  return it ? (it.emoji ? it.emoji + " " : "") + it.name : foodId;
}

// ── new-item modal (leaf vs parent-with-sub-options) — shared, all lists ─
let _pendingNewItem = { name: "", listKind: "items" };
function openNewItemModal(name, listKind = "items") {
  _pendingNewItem = { name, listKind };
  $("#newitem-name").textContent = name;
  $("#newitem-parent").classList.toggle("hidden", listKind !== "items");
  $("#newitem-modal").classList.remove("hidden");
}
function closeNewItemModal() { $("#newitem-modal").classList.add("hidden"); }
async function createNewItemAsLeaf() {
  const { name, listKind } = _pendingNewItem;
  closeNewItemModal();
  try {
    if (listKind === "items") {
      const item = await apiJson("/api/items", "POST", { name });
      await loadItems();
      const fresh = state.items.find((i) => i._id === item._id) || item;
      selectFoodItem(fresh, false);
    } else if (listKind === "symptom_items") {
      const created = await apiJson("/api/symptom-items", "POST", { name });
      await loadSymptomItems();
      state.selectedSymptoms.set(created._id, created.name);
      renderSymptomTypeahead(); renderSelectedSymptoms();
    } else if (listKind === "bathroom_items") {
      const created = await apiJson("/api/bathroom-items", "POST", { name });
      await loadBathroomItems();
      state.selectedBathroomKind = created;
      $("#bathroom-selected").textContent = created.name;
      renderBathroomTypeahead();
    }
  } catch (err) {
    toast("Could not add item: " + err.message, "error");
  }
}
async function createNewItemAsParent() {
  const { name } = _pendingNewItem;
  closeNewItemModal();
  try {
    const item = await apiJson("/api/items", "POST", { name });
    await loadItems();
    const fresh = state.items.find((i) => i._id === item._id) || item;
    openSuboptionModal(fresh, { alreadyAddedParent: false });
  } catch (err) {
    toast("Could not add item: " + err.message, "error");
  }
}

// ── sub-option popup (recursive, additive; suggestion only) ──────────────
function openSuboptionModal(parentItem, { alreadyAddedParent }) {
  state.subOptionCtx = { parentItem, ticked: new Map(), alreadyAddedParent };
  $("#suboption-title").textContent = `Sub-options for "${parentItem.name}"`;
  $("#suboption-input").value = "";
  renderSuboptionList();
  $("#suboption-modal").classList.remove("hidden");
}
function closeSuboptionModal() { $("#suboption-modal").classList.add("hidden"); state.subOptionCtx = null; }
function renderSuboptionList() {
  const ctx = state.subOptionCtx;
  if (!ctx) return;
  const q = $("#suboption-input").value;
  const kids = childrenOf(ctx.parentItem._id);
  const pool = (kids.length && !q) ? kids : state.items.filter((i) => i._id !== ctx.parentItem._id);
  const candidates = filterByName(pool, q);
  candidates.__all = state.items;
  const list = $("#suboption-list");
  list.innerHTML = "";
  for (const it of candidates) {
    const chip = document.createElement("button");
    chip.type = "button";
    const ticked = ctx.ticked.has(it._id);
    chip.className = "chip" + (ticked ? " selected" : "");
    chip.textContent = (ticked ? "✓ " : "") + it.name;
    chip.addEventListener("click", () => {
      if (ctx.ticked.has(it._id)) ctx.ticked.delete(it._id); else ctx.ticked.set(it._id, it.name);
      renderSuboptionList();
    });
    list.appendChild(chip);
  }
  if (q.trim() && !exactMatch(state.items, q)) {
    const add = document.createElement("button");
    add.type = "button"; add.className = "chip new";
    add.textContent = `+ Add & link "${q.trim()}"`;
    add.addEventListener("click", async () => {
      try {
        const created = await apiJson("/api/items", "POST", { name: q.trim() });
        await apiJson("/api/item-links", "POST", { child: created._id, parent: ctx.parentItem._id });
        await loadItems();
        ctx.ticked.set(created._id, created.name);
        $("#suboption-input").value = "";
        renderSuboptionList();
      } catch (err) {
        toast("Could not add sub-option: " + err.message, "error");
      }
    });
    list.appendChild(add);
  }
}
async function skipSuboptions() {
  const ctx = state.subOptionCtx;
  if (!ctx) return;
  if (!ctx.alreadyAddedParent) addToDraft(ctx.parentItem);
  closeSuboptionModal();
}
async function confirmSuboptions() {
  const ctx = state.subOptionCtx;
  if (!ctx) return;
  if (!ctx.alreadyAddedParent) addToDraft(ctx.parentItem);
  try {
    for (const [id, name] of ctx.ticked) {
      const existingLink = state.itemLinks.some((l) => l.child === id && l.parent === ctx.parentItem._id);
      if (!existingLink) {
        await apiJson("/api/item-links", "POST", { child: id, parent: ctx.parentItem._id });
      }
      addToDraft({ _id: id, name });
    }
    await loadItems();
  } catch (err) {
    toast("Could not save sub-options: " + err.message, "error");
  }
  closeSuboptionModal();
}

// ── holidays ─────────────────────────────────────────────────────────────
async function loadHolidays() {
  const rows = await api("/api/holidays");
  const ul = $("#holiday-list");
  ul.innerHTML = "";
  for (const h of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="item-main"><strong>${escapeHtml(h.name || h.location)}</strong>
      <span class="meta">${h.start} → ${h.stop} · ${escapeHtml(h.location)}</span></div>`;
    if (state.principal.role === "owner") {
      const actions = document.createElement("div");
      actions.className = "actions";
      const del = document.createElement("button");
      del.type = "button"; del.className = "small ghost"; del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm("Delete this holiday?")) return;
        await api(`/api/holidays/${h._id}`, { method: "DELETE" });
        toast("Holiday deleted.", "success");
        await Promise.all([loadHolidays(), loadTimeline()]);
      });
      actions.appendChild(del);
      li.appendChild(actions);
    }
    ul.appendChild(li);
  }
  return rows;
}
async function addHoliday() {
  const start = $("#holiday-start").value, stop = $("#holiday-stop").value;
  const location = $("#holiday-location").value.trim();
  if (!start || !stop || !location) { toast("From, to, and location are required.", "error"); return; }
  try {
    await apiJson("/api/holidays", "POST", {
      start, stop, location, name: $("#holiday-name").value.trim() || null,
    });
    $("#holiday-start").value = ""; $("#holiday-stop").value = "";
    $("#holiday-location").value = ""; $("#holiday-name").value = "";
    toast("Holiday added.", "success");
    await Promise.all([loadHolidays(), loadTimeline()]);
  } catch (err) {
    toast("Could not add holiday: " + err.message, "error");
  }
}

// ── daily check-in (temp / energy / sleep / pain / symptoms) ────────────
function setTempDisplay() {
  const v = $("#temp-input").value;
  const disp = $("#temp-display");
  disp.innerHTML = v ? `${parseFloat(v).toFixed(1)}<span class="unit">°C</span>` : `——<span class="unit">°C</span>`;
  disp.classList.toggle("fever", !!v && parseFloat(v) >= (state.config?.fever_threshold ?? 37.8));
}
function stepTemp(delta) {
  const cur = parseFloat($("#temp-input").value || "37.0");
  $("#temp-input").value = (Math.round((cur + delta) * 10) / 10).toFixed(1);
  setTempDisplay();
}
function updateEnergyLabel() {
  $("#energy-label").textContent = ENERGY_LABELS[parseInt($("#energy-range").value, 10)] ?? "Normal";
}

const PAIN_CYCLE = SEVERITY_CYCLE;
// pain_map keys = data-region ids in index.html (see the comment above the
// body-map SVGs for the clinical convention they follow). Left/right are
// the person's own sides. Used for readable timeline text only — the stored
// key is always the snake_case id.
const PAIN_REGION_LABELS = {
  head_frontal: "forehead", head_temporal_left: "left temple", head_temporal_right: "right temple",
  head_vertex: "top of head", head_occipital: "back of head", head_face: "face",
  neck: "neck", shoulder_left: "left shoulder", shoulder_right: "right shoulder",
  chest: "chest", epigastric: "upper middle abdomen", periumbilical: "around the navel",
  suprapubic: "lower middle abdomen",
  abdomen_upper_left: "upper left abdomen", abdomen_upper_right: "upper right abdomen",
  abdomen_lower_left: "lower left abdomen", abdomen_lower_right: "lower right abdomen",
  back_upper_left: "upper back (left)", back_upper_right: "upper back (right)",
  back_mid_left: "mid back (left)", back_mid_right: "mid back (right)",
  back_lower_left: "lower back (left)", back_lower_right: "lower back (right)",
  hip_left: "left hip", hip_right: "right hip",
  arm_upper_left: "left upper arm", arm_upper_right: "right upper arm",
  elbow_left: "left elbow", elbow_right: "right elbow",
  forearm_left: "left forearm", forearm_right: "right forearm",
  hand_left: "left hand", hand_right: "right hand",
  thigh_left: "left thigh", thigh_right: "right thigh",
  knee_left: "left knee", knee_right: "right knee",
  calf_left: "left calf", calf_right: "right calf",
  foot_left: "left foot", foot_right: "right foot",
};
// Keys written by the first body map (before the region split). Translated
// on load so an older day still shows its painted regions; the day is then
// saved in the new vocabulary the next time it is saved. Keys not listed
// here (and not on the map) are kept as-is so nothing is silently dropped.
const LEGACY_PAIN_REGIONS = {
  head: ["head_frontal"],
  arm_left: ["arm_upper_left"], arm_right: ["arm_upper_right"],
  leg_left: ["thigh_left"], leg_right: ["thigh_right"],
  back_upper: ["back_upper_left", "back_upper_right"],
  back_mid: ["back_mid_left", "back_mid_right"],
  back_lower: ["back_lower_left", "back_lower_right"],
};
function migratePainMap(map) {
  const out = {};
  for (const [key, sev] of Object.entries(map || {})) {
    const targets = LEGACY_PAIN_REGIONS[key] || [key];
    for (const t of targets) if (!out[t]) out[t] = sev;
  }
  return out;
}
function painRegionLabel(key) {
  return PAIN_REGION_LABELS[key] || key.replace(/_/g, " ");
}
function applyPainRegionVisual(region) {
  const sev = state.painMap[region] || "";
  $$(`.pain-region[data-region="${region}"]`).forEach((el) => {
    if (sev) el.setAttribute("data-severity", sev); else el.removeAttribute("data-severity");
  });
}
function applyAllPainVisuals() {
  const regions = new Set($$(".pain-region").map((el) => el.dataset.region));
  regions.forEach(applyPainRegionVisual);
}
function bindPainRegions() {
  $$(".pain-region").forEach((el) => {
    el.addEventListener("click", () => {
      const region = el.dataset.region;
      const cur = state.painMap[region] || null;
      const next = PAIN_CYCLE[(PAIN_CYCLE.indexOf(cur) + 1) % PAIN_CYCLE.length];
      if (next) state.painMap[region] = next; else delete state.painMap[region];
      applyPainRegionVisual(region);
    });
  });
}
function setBodyView(view) {
  $("#body-front").classList.toggle("hidden", view !== "front");
  $("#body-back").classList.toggle("hidden", view !== "back");
  $("#body-tab-front").classList.toggle("ghost", view !== "front");
  $("#body-tab-back").classList.toggle("ghost", view !== "back");
  $("#body-tab-front").setAttribute("aria-selected", String(view === "front"));
  $("#body-tab-back").setAttribute("aria-selected", String(view === "back"));
}

async function loadSymptomItems() {
  state.symptomItems = await api("/api/symptom-items");
  renderSymptomTypeahead();
}
function renderSymptomTypeahead() {
  const q = $("#symptom-input").value;
  const matches = filterByName(state.symptomItems, q);
  matches.__all = state.symptomItems;
  renderChips($("#symptom-list"), matches, {
    query: q,
    isSelected: (it) => state.selectedSymptoms.has(it._id),
    onTap: (it) => {
      if (state.selectedSymptoms.has(it._id)) state.selectedSymptoms.delete(it._id);
      else state.selectedSymptoms.set(it._id, it.name);
      $("#symptom-input").value = "";
      renderSymptomTypeahead(); renderSelectedSymptoms();
    },
    onAddNew: (name) => openNewItemModal(name, "symptom_items"),
  });
}
function renderSelectedSymptoms() {
  const wrap = $("#symptom-selected");
  wrap.innerHTML = "";
  for (const [id, name] of state.selectedSymptoms) {
    const chip = document.createElement("button");
    chip.type = "button"; chip.className = "chip selected"; chip.textContent = name + " ✕";
    chip.addEventListener("click", () => { state.selectedSymptoms.delete(id); renderSelectedSymptoms(); renderSymptomTypeahead(); });
    wrap.appendChild(chip);
  }
}

async function loadTodayHealth() {
  const today = todayDateKey();
  const rows = await api(`/api/health?date=${today}`);
  if (!rows.length) {
    $("#routine-event-at").value = nowLocalInput();
    return;
  }
  const h = rows[0];
  $("#routine-event-at").value = h.event_at ? isoToLocalInput(h.event_at) : nowLocalInput();
  $("#temp-input").value = h.temperature_celsius != null ? h.temperature_celsius : "";
  setTempDisplay();
  if (h.energy != null) $("#energy-range").value = h.energy;
  updateEnergyLabel();
  $("#sleep-input").value = h.sleep_hours != null ? h.sleep_hours : "";
  $("#pain-scale").value = h.pain_scale != null ? h.pain_scale : "";
  state.painMap = migratePainMap(h.pain_map);
  applyAllPainVisuals();
  $("#routine-notes").value = h.notes || "";
  state.selectedSymptoms = new Map();
  for (const s of h.symptoms || []) {
    const item = state.symptomItems.find((i) => i._id === s.symptom_id);
    state.selectedSymptoms.set(s.symptom_id, item ? item.name : s.symptom_id);
  }
  renderSelectedSymptoms();
}
async function saveRoutine() {
  try {
    await apiJson("/api/health", "POST", {
      event_at: toIso($("#routine-event-at").value),
      temperature_celsius: $("#temp-input").value ? parseFloat($("#temp-input").value) : null,
      energy: parseInt($("#energy-range").value, 10),
      sleep_hours: $("#sleep-input").value ? parseFloat($("#sleep-input").value) : null,
      symptom_ids: Array.from(state.selectedSymptoms.keys()),
      pain_map: state.painMap,
      pain_scale: $("#pain-scale").value ? parseInt($("#pain-scale").value, 10) : null,
      notes: $("#routine-notes").value.trim() || null,
    });
    toast("Check-in saved.", "success");
    await loadTimeline();
  } catch (err) {
    toast("Could not save check-in: " + err.message, "error");
  }
}

// ── bathroom events ──────────────────────────────────────────────────────
async function loadBathroomItems() {
  state.bathroomItems = await api("/api/bathroom-items");
  renderBathroomTypeahead();
}
function renderBathroomTypeahead() {
  const q = $("#bathroom-input").value;
  const matches = filterByName(state.bathroomItems, q);
  matches.__all = state.bathroomItems;
  renderChips($("#bathroom-list"), matches, {
    query: q,
    isSelected: (it) => !!state.selectedBathroomKind && state.selectedBathroomKind._id === it._id,
    onTap: (it) => {
      state.selectedBathroomKind = it;
      $("#bathroom-selected").textContent = it.name;
      $("#bathroom-input").value = "";
      renderBathroomTypeahead();
    },
    onAddNew: (name) => openNewItemModal(name, "bathroom_items"),
  });
}
async function logBathroomEvent() {
  if (!state.selectedBathroomKind) { toast("Pick or type a kind first.", "error"); return; }
  const fd = new FormData();
  fd.set("kind", state.selectedBathroomKind._id);
  const eventAt = toIso($("#be-event-at").value);
  if (eventAt) fd.set("event_at", eventAt);
  fd.set("notes", $("#be-notes").value.trim());
  const file = $("#be-photo").files[0];
  if (file) fd.set("photo", file);
  try {
    const res = await fetch("/api/bathroom-events", { method: "POST", credentials: "include", body: fd });
    if (res.status === 401) { showLoginGate(); return; }
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Failed to log event"); }
    toast("Event logged.", "success");
    state.selectedBathroomKind = null;
    $("#bathroom-selected").textContent = "none";
    $("#be-notes").value = ""; $("#be-photo").value = ""; $("#be-event-at").value = nowLocalInput();
    await Promise.all([loadBathroomLog(), loadTimeline()]);
  } catch (err) {
    toast("Could not log event: " + err.message, "error");
  }
}
async function loadBathroomLog() {
  const today = todayDateKey();
  const rows = await api(`/api/bathroom-events?date=${today}`);
  const ul = $("#bathroom-log-list");
  ul.innerHTML = "";
  $("#bathroom-empty").classList.toggle("hidden", rows.length > 0);
  for (const doc of rows) {
    const kind = state.bathroomItems.find((k) => k._id === doc.kind);
    const li = document.createElement("li");
    li.innerHTML = `<div class="item-main"><strong>${escapeHtml(kind ? kind.name : doc.kind)}</strong>
      <span class="meta">${fmtTime(doc.event_at)}${doc.notes ? " · " + escapeHtml(doc.notes) : ""}</span></div>`;
    if (state.principal.role === "owner") {
      const actions = document.createElement("div");
      actions.className = "actions";
      const del = document.createElement("button");
      del.type = "button"; del.className = "small ghost"; del.textContent = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm("Delete this event?")) return;
        await api(`/api/bathroom-events/${doc._id}`, { method: "DELETE" });
        toast("Event deleted.", "success");
        await Promise.all([loadBathroomLog(), loadTimeline()]);
      });
      actions.appendChild(del);
      li.appendChild(actions);
    }
    ul.appendChild(li);
  }
}

// ── notes ────────────────────────────────────────────────────────────────
async function addNote() {
  const text = $("#note-input").value.trim();
  if (!text) return;
  try {
    await apiJson("/api/notes", "POST", { text, event_at: toIso($("#note-event-at").value) });
    $("#note-input").value = "";
    $("#note-event-at").value = nowLocalInput();
    toast("Note added.", "success");
    await loadTimeline();
  } catch (err) {
    toast("Could not add note: " + err.message, "error");
  }
}

// ── timeline ─────────────────────────────────────────────────────────────
function timelineTitle(entry) {
  if (entry.type === "meal") return entry.doc.lines.map((l) => itemLabel(l.food_id)).join(", ");
  if (entry.type === "routine") {
    const parts = [];
    if (entry.doc.temperature_celsius != null) parts.push(`${entry.doc.temperature_celsius}°C`);
    if (entry.doc.energy != null) parts.push(ENERGY_LABELS[entry.doc.energy]);
    if (entry.doc.sleep_hours != null) parts.push(`${entry.doc.sleep_hours}h sleep`);
    return "Daily check-in" + (parts.length ? ` — ${parts.join(", ")}` : "");
  }
  if (entry.type === "bathroom") {
    const kind = state.bathroomItems.find((k) => k._id === entry.doc.kind);
    return kind ? kind.name : entry.doc.kind;
  }
  if (entry.type === "note") return entry.doc.text;
  return "Event";
}
function timelineDetail(entry) {
  if (entry.type === "meal") {
    const where = entry.doc.lines[0].where === "out_prepared" ? "Out-prepared" : "Home-prepared";
    const wn = entry.doc.lines[0].where_name ? ` @ ${entry.doc.lines[0].where_name}` : "";
    return where + wn;
  }
  if (entry.type === "routine") {
    const symptoms = (entry.doc.symptoms || []).map((s) => {
      const it = state.symptomItems.find((i) => i._id === s.symptom_id);
      return it ? it.name : s.symptom_id;
    }).join(", ");
    const bits = [];
    if (symptoms) bits.push(`Symptoms: ${symptoms}`);
    if (entry.doc.pain_scale != null) bits.push(`Pain ${entry.doc.pain_scale}/10`);
    const painRegions = Object.entries(entry.doc.pain_map || {})
      .map(([k, sev]) => `${painRegionLabel(k)} (${sev})`);
    if (painRegions.length) bits.push(`Pain: ${painRegions.join(", ")}`);
    return bits.join(" · ");
  }
  if (entry.type === "bathroom") return entry.doc.notes || "";
  return "";
}
async function deleteTimelineEntry(entry) {
  if (!confirm("Delete this?")) return;
  if (entry.type === "meal") await api(`/api/intake/group/${entry.doc.intake_id}`, { method: "DELETE" });
  else if (entry.type === "bathroom") await api(`/api/bathroom-events/${entry.doc._id}`, { method: "DELETE" });
  else if (entry.type === "note") await api(`/api/notes/${entry.doc._id}`, { method: "DELETE" });
  else return; // routines are edited, not deleted, from the timeline
  toast("Deleted.", "success");
  await loadTimeline();
}
async function loadTimeline() {
  const from = daysAgoIso(14).slice(0, 10);
  const to = todayDateKey();
  const data = await api(`/api/timeline?frm=${from}&to=${to}`);

  const bandHost = $("#holiday-bands");
  bandHost.innerHTML = "";
  for (const h of data.holidays || []) {
    const band = document.createElement("div");
    band.className = "holiday-band";
    band.textContent = `${h.name || "Holiday"} — ${h.start} to ${h.stop} (${h.location})`;
    bandHost.appendChild(band);
  }

  const list = $("#timeline-list");
  list.innerHTML = "";
  const items = data.items || [];
  $("#timeline-empty").classList.toggle("hidden", items.length > 0);

  const byDay = new Map();
  items.slice().reverse().forEach((item) => {
    const key = dateKey(item.event_at);
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(item);
  });
  for (const [day, dayItems] of byDay.entries()) {
    const group = document.createElement("div");
    group.className = "timeline-day-group";
    const heading = document.createElement("div");
    heading.className = "timeline-day-heading";
    heading.textContent = fmtDateHeading(day + "T00:00:00Z");
    group.appendChild(heading);
    for (const entry of dayItems) {
      const row = document.createElement("div");
      row.className = "timeline-item";
      const detail = timelineDetail(entry);
      row.innerHTML = `
        <div class="timeline-time">${fmtTime(entry.event_at)}</div>
        <div class="timeline-icon">${TYPE_ICON[entry.type] || "•"}</div>
        <div class="timeline-body">
          <div class="timeline-title">${escapeHtml(timelineTitle(entry))}</div>
          ${detail ? `<div class="timeline-detail">${escapeHtml(detail)}</div>` : ""}
          <div class="timeline-actions"></div>
        </div>`;
      if (entry.type === "bathroom" && entry.doc.photo) {
        const img = document.createElement("img");
        img.className = "photo-thumb";
        img.alt = "Bathroom event photo";
        loadPhotoBlobUrl(`/api/bathroom-events/${entry.doc._id}/photo`).then((url) => { if (url) img.src = url; });
        row.querySelector(".timeline-body").appendChild(img);
      }
      if (state.principal.role === "owner" && entry.type !== "routine") {
        const actionsWrap = row.querySelector(".timeline-actions");
        const del = document.createElement("button");
        del.type = "button"; del.className = "small ghost"; del.textContent = "Delete";
        del.addEventListener("click", () => deleteTimelineEntry(entry));
        actionsWrap.appendChild(del);
      }
      group.appendChild(row);
    }
    list.appendChild(group);
  }
}

// ── ask / findings ───────────────────────────────────────────────────────
async function runAsk(save) {
  const question = $("#ask-question").value.trim();
  if (!question) { toast("Type a question first.", "error"); return; }
  try {
    const res = await apiJson("/api/ask", "POST", {
      question,
      frm: $("#ask-from").value || null,
      to: $("#ask-to").value || null,
      save,
    });
    $("#ask-result").classList.remove("hidden");
    $("#ask-result").textContent = res.context;
    if (save) { toast("Saved to findings.", "success"); await loadFindings(); }
  } catch (err) {
    toast("Ask failed: " + err.message, "error");
  }
}
async function loadFindings() {
  const rows = await api("/api/findings");
  const ul = $("#findings-list");
  ul.innerHTML = "";
  $("#findings-empty").classList.toggle("hidden", rows.length > 0);
  for (const f of rows) {
    const li = document.createElement("li");
    li.className = "finding-card";
    const title = f.title || f.question || "(untitled)";
    const bodyLines = (f.findings || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
    li.innerHTML = `
      <div class="finding-title">${escapeHtml(title)}</div>
      <div class="finding-meta">${f.created_at ? new Date(f.created_at).toLocaleString() : ""}${f.author ? " · " + escapeHtml(f.author) : ""}</div>
      ${f.answer ? `<p>${escapeHtml(f.answer)}</p>` : ""}
      ${bodyLines ? `<ul>${bodyLines}</ul>` : ""}`;
    ul.appendChild(li);
  }
}

// ── settings / passkeys ──────────────────────────────────────────────────
async function loadSecuritySection() {
  const show = state.principal.role === "owner" && state.config.auth_mode === "public" && state.config.passkeys_enabled;
  $("#security-section").classList.toggle("hidden", !show);
  if (!show) return;
  const rows = await api("/api/auth/passkeys");
  const ul = $("#passkey-list");
  ul.innerHTML = "";
  for (const pk of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="item-main"><strong>${escapeHtml(pk.nickname || "Passkey")}</strong>
      <span class="meta">added ${pk.created_at ? new Date(pk.created_at).toLocaleDateString() : ""}</span></div>`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const del = document.createElement("button");
    del.type = "button"; del.className = "small ghost"; del.textContent = "Remove";
    del.addEventListener("click", async () => {
      if (!confirm("Remove this passkey?")) return;
      await api(`/api/auth/passkeys/${pk._id}`, { method: "DELETE" });
      toast("Passkey removed.", "success");
      await loadSecuritySection();
    });
    actions.appendChild(del);
    li.appendChild(actions);
    ul.appendChild(li);
  }
}
async function addPasskey() {
  try {
    if (!window.PublicKeyCredential) throw new Error("This browser doesn't support passkeys.");
    const options = await api("/api/auth/passkeys/register/begin", { method: "POST" });
    const cred = await navigator.credentials.create({ publicKey: decodeCreationOptions(options) });
    const nickname = prompt('Name this passkey (e.g. "iPhone")', "") || "Passkey";
    await apiJson("/api/auth/passkeys/register/complete", "POST", {
      nickname, credential: credentialToJson(cred),
    });
    toast("Passkey added.", "success");
    await loadSecuritySection();
  } catch (err) {
    toast("Could not add passkey: " + err.message, "error");
  }
}

// ── boot ─────────────────────────────────────────────────────────────────
async function bootApp() {
  goToTab("today");
  bindPainRegions();
  setTempDisplay();
  updateEnergyLabel();
  $("#intake-consumed-at").value = nowLocalInput();
  $("#be-event-at").value = nowLocalInput();
  $("#note-event-at").value = nowLocalInput();
  await Promise.all([loadItems(), loadSymptomItems(), loadBathroomItems()]);
  await Promise.all([loadIntakeLog(), loadBathroomLog(), loadHolidays(), loadTodayHealth(), loadTimeline(), loadFindings()]);
  await loadSecuritySection();
}

function wireEvents() {
  $("#login-form").addEventListener("submit", handleLoginSubmit);
  $("#passkey-login-btn").addEventListener("click", () => {
    toast("Sign in with your password first — a passkey is a second factor.", "");
  });
  $("#logout-btn").addEventListener("click", handleLogout);
  $("#theme-btn").addEventListener("click", toggleTheme);

  $$(".tab-item").forEach((btn) => btn.addEventListener("click", () => goToTab(btn.dataset.target)));

  $("#food-input").addEventListener("input", renderFoodTypeahead);
  $("#save-intake").addEventListener("click", saveIntake);
  $("#clear-intake").addEventListener("click", clearIntakeForm);
  $("#intake-where").addEventListener("change", () => {
    $("#where-name-field").classList.toggle("hidden", $("#intake-where").value !== "out_prepared");
  });

  $("#suboption-input").addEventListener("input", renderSuboptionList);
  $("#suboption-done").addEventListener("click", confirmSuboptions);
  $("#suboption-skip").addEventListener("click", skipSuboptions);

  $("#newitem-leaf").addEventListener("click", createNewItemAsLeaf);
  $("#newitem-parent").addEventListener("click", createNewItemAsParent);
  $("#newitem-cancel").addEventListener("click", closeNewItemModal);

  $("#temp-input").addEventListener("input", setTempDisplay);
  $("#temp-minus").addEventListener("click", () => stepTemp(-0.1));
  $("#temp-plus").addEventListener("click", () => stepTemp(0.1));
  $("#energy-range").addEventListener("input", updateEnergyLabel);
  $("#body-tab-front").addEventListener("click", () => setBodyView("front"));
  $("#body-tab-back").addEventListener("click", () => setBodyView("back"));
  $("#symptom-input").addEventListener("input", renderSymptomTypeahead);
  $("#save-routine").addEventListener("click", saveRoutine);

  $("#bathroom-input").addEventListener("input", renderBathroomTypeahead);
  $("#log-be").addEventListener("click", logBathroomEvent);

  $("#add-note").addEventListener("click", addNote);
  $("#add-holiday").addEventListener("click", addHoliday);

  $("#ask-btn").addEventListener("click", () => runAsk(false));
  $("#ask-save-btn").addEventListener("click", () => runAsk(true));

  $("#add-passkey-btn").addEventListener("click", addPasskey);
}

document.addEventListener("DOMContentLoaded", () => {
  applyStoredTheme();
  wireEvents();
  tryAuthAndEnter();
});
