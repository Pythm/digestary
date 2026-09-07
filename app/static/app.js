// Digestary — vanilla JS frontend, no build step, no framework.
"use strict";

// ── state ────────────────────────────────────────────────────────────────
const state = {
  config: null,
  principal: null,         // { username, role }
  items: [],
  itemLinks: [],
  symptomItems: [],
  bathroomItems: [],
  mealDraft: [],           // [{food_id, name}]
  selectedSymptoms: new Map(),   // id -> name
  selectedBathroomKind: null,    // {_id, name}
  painMap: {},             // region -> "yellow"|"orange"|"red"
  subOptionCtx: null,      // {parentItem, ticked:Map(id->name), alreadyAddedParent}
};
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
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
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
  badge.textContent = `👑 ${state.principal.username}`;
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

$("#login-submit").addEventListener("click", async () => {
  $("#login-error").textContent = "";
  try {
    const res = await apiJson("/api/auth/login", "POST", {
      username: $("#login-username").value, password: $("#login-password").value,
    });
    state.principal = res.mfa_required ? await completePasskeyLogin(res) : res;
    showApp();
    updateRoleBadge();
    await bootApp();
  } catch (e) {
    $("#login-error").textContent = e.passkeyStep
      ? "Passkey verification failed or was cancelled."
      : "Invalid username/password, or too many attempts.";
  }
});
$("#logout-btn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  location.reload();
});

// ── passkey login step (2nd factor, only asked of accounts that enrolled
// one — see #security-section) ─────────────────────────────────────────
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
    $("#login-sub").textContent = "Sign in to your journal.";
  }
}

// ── WebAuthn (passkeys) — base64url <-> ArrayBuffer plumbing the browser's
// PublicKeyCredential API needs; the server only ever speaks base64url JSON
// (see webauthn.helpers.options_to_json / parse_*_credential_json server-side).
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

// ── security — passkey enrollment/management (owner, public mode only) ───
async function loadSecuritySection() {
  const show = state.principal.role === "owner" && state.config.auth_mode === "public" && state.config.passkeys_enabled;
  $("#security-section").classList.toggle("feature-off", !show);
  if (!show) return;
  const rows = await api("/api/auth/passkeys");
  const ul = $("#passkey-list");
  ul.innerHTML = "";
  for (const pk of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<div>${escapeHtml(pk.nickname || "Passkey")}<div class="meta">added ${fmtTime(pk.created_at)}</div></div>`;
    const del = document.createElement("button");
    del.className = "small ghost"; del.textContent = "🗑";
    del.addEventListener("click", async () => {
      if (!confirm("Remove this passkey?")) return;
      await api(`/api/auth/passkeys/${pk._id}`, { method: "DELETE" });
      await loadSecuritySection();
    });
    li.appendChild(del);
    ul.appendChild(li);
  }
}
$("#add-passkey-btn").addEventListener("click", async () => {
  try {
    if (!window.PublicKeyCredential) throw new Error("This browser doesn't support passkeys.");
    const options = await api("/api/auth/passkeys/register/begin", { method: "POST" });
    const cred = await navigator.credentials.create({ publicKey: decodeCreationOptions(options) });
    const nickname = prompt('Name this passkey (e.g. "iPhone")', "") || "Passkey";
    await apiJson("/api/auth/passkeys/register/complete", "POST", {
      nickname, credential: credentialToJson(cred),
    });
    toast("Passkey added");
    await loadSecuritySection();
  } catch (e) {
    toast("Could not add passkey: " + e.message, "error");
  }
});

// ── sidebar / page navigation ───────────────────────────────────────────
function openSidebar() {
  $("#sidebar").classList.add("open");
  $("#sidebar-backdrop").classList.remove("hidden");
}
function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebar-backdrop").classList.add("hidden");
}
$("#menu-btn").addEventListener("click", openSidebar);
$("#sidebar-close").addEventListener("click", closeSidebar);
$("#sidebar-backdrop").addEventListener("click", closeSidebar);

function showPage(page) {
  $$("[data-page]").forEach((el) => el.classList.toggle("hidden", el.dataset.page !== page));
  $$(".nav-item").forEach((btn) => btn.classList.toggle("active", btn.dataset.target === page));
  closeSidebar();
  window.scrollTo(0, 0);
}
$$(".nav-item").forEach((btn) => btn.addEventListener("click", () => showPage(btn.dataset.target)));

// ── theme ────────────────────────────────────────────────────────────────
function applyStoredTheme() {
  try {
    const t = localStorage.getItem("digestary_theme");
    if (t) document.documentElement.dataset.theme = t;
  } catch (_) {}
}
$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("digestary_theme", next); } catch (_) {}
});

// ── generic type-ahead ───────────────────────────────────────────────────
function filterByName(list, query) {
  const q = query.trim().toLowerCase();
  if (!q) return list.slice(0, 30);
  return list.filter((i) => i.name.toLowerCase().includes(q)).slice(0, 30);
}
function exactMatch(list, query) {
  const q = query.trim().toLowerCase();
  return list.find((i) => i.name.toLowerCase() === q);
}
function renderChips(listEl, items, { onTap, query, onAddNew, emoji }) {
  listEl.innerHTML = "";
  for (const it of items) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = (emoji && it.emoji ? it.emoji + " " : "") + it.name;
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
    onTap: (it) => selectFoodItem(it, false),
    onAddNew: (name) => openNewItemModal(name),
  });
}
$("#food-input").addEventListener("input", renderFoodTypeahead);

function addToDraft(item) {
  if (state.mealDraft.some((d) => d.food_id === item._id)) return;
  state.mealDraft.push({ food_id: item._id, name: item.name });
  renderMealDraft();
}
function renderMealDraft() {
  const ul = $("#meal-draft-list");
  ul.innerHTML = "";
  for (const d of state.mealDraft) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(d.name)}</span>`;
    const rm = document.createElement("button");
    rm.className = "small ghost"; rm.textContent = "✕";
    rm.addEventListener("click", () => {
      state.mealDraft = state.mealDraft.filter((x) => x.food_id !== d.food_id);
      renderMealDraft();
    });
    li.appendChild(rm);
    ul.appendChild(li);
  }
}
function selectFoodItem(item, alreadyPlannedForSubpopup) {
  addToDraft(item);
  $("#food-input").value = "";
  renderFoodTypeahead();
  if (item.is_parent && !alreadyPlannedForSubpopup) {
    openSuboptionModal(item, { alreadyAddedParent: true });
  }
}

$("#intake-where").addEventListener("change", () => {
  $("#where-name-field").hidden = $("#intake-where").value !== "out_prepared";
});

$("#clear-intake").addEventListener("click", clearIntakeForm);
function clearIntakeForm() {
  state.mealDraft = [];
  renderMealDraft();
  $("#intake-notes").value = "";
  $("#intake-where-name").value = "";
  $("#intake-consumed-at").value = nowLocalInput();
}

$("#save-intake").addEventListener("click", async () => {
  if (!state.mealDraft.length) { toast("Add at least one food first", "error"); return; }
  await api("/api/intake", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      food_ids: state.mealDraft.map((d) => d.food_id),
      consumed_at: toIso($("#intake-consumed-at").value),
      where: $("#intake-where").value,
      where_name: $("#intake-where-name").value || null,
      notes: $("#intake-notes").value,
    }),
  });
  toast("Meal saved");
  clearIntakeForm();
  await Promise.all([loadIntakeLog(), loadTimeline()]);
});

async function loadIntakeLog() {
  const rows = await api(`/api/intake?frm=${encodeURIComponent(daysAgoIso(14))}`);
  const groups = new Map();
  for (const line of rows) {
    if (!groups.has(line.intake_id)) groups.set(line.intake_id, []);
    groups.get(line.intake_id).push(line);
  }
  const ul = $("#intake-log-list");
  ul.innerHTML = "";
  const sorted = Array.from(groups.entries()).sort((a, b) => (b[1][0].consumed_at < a[1][0].consumed_at ? -1 : 1));
  for (const [intakeId, lines] of sorted) {
    const names = lines.map((l) => (state.items.find((i) => i._id === l.food_id)?.name) || l.food_id).join(", ");
    const li = document.createElement("li");
    li.innerHTML = `<div><div>${escapeHtml(names)}</div>
      <div class="meta">${fmtTime(lines[0].consumed_at)} · ${escapeHtml(lines[0].where || "")}${lines[0].where_name ? " @ " + escapeHtml(lines[0].where_name) : ""}</div></div>`;
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.appendChild(editTimeButton(lines[0].consumed_at, async (iso) => {
      await apiJson(`/api/intake/group/${intakeId}`, "PATCH", { consumed_at: iso });
      await loadIntakeLog(); await loadTimeline();
    }));
    if (state.principal.role === "owner") {
      const del = document.createElement("button");
      del.className = "small ghost"; del.textContent = "🗑";
      del.addEventListener("click", async () => {
        if (!confirm("Delete this whole meal?")) return;
        await api(`/api/intake/group/${intakeId}`, { method: "DELETE" });
        await loadIntakeLog(); await loadTimeline();
      });
      actions.appendChild(del);
    }
    li.appendChild(actions);
    ul.appendChild(li);
  }
}
function apiJson(path, method, body) {
  return api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
function editTimeButton(currentIso, onSave) {
  const btn = document.createElement("button");
  btn.className = "small ghost"; btn.textContent = "🕘";
  btn.addEventListener("click", () => {
    const existing = btn.parentElement.querySelector(".inline-time-editor");
    if (existing) { existing.remove(); return; }
    const wrap = document.createElement("span");
    wrap.className = "inline-time-editor";
    const input = document.createElement("input");
    input.type = "datetime-local"; input.value = isoToLocalInput(currentIso);
    input.style.minWidth = "180px";
    const ok = document.createElement("button");
    ok.className = "small"; ok.textContent = "✓";
    ok.addEventListener("click", async () => { await onSave(toIso(input.value)); wrap.remove(); });
    wrap.appendChild(input); wrap.appendChild(ok);
    btn.parentElement.appendChild(wrap);
  });
  return btn;
}

// ── new-item modal (leaf vs parent-with-sub-options) — food only ─────────
let _pendingNewItemName = "";
function openNewItemModal(name) {
  _pendingNewItemName = name;
  $("#newitem-name").textContent = name;
  $("#newitem-modal").classList.remove("hidden");
}
function closeNewItemModal() { $("#newitem-modal").classList.add("hidden"); }
$("#newitem-cancel").addEventListener("click", closeNewItemModal);
$("#newitem-leaf").addEventListener("click", async () => {
  const item = await apiJson("/api/items", "POST", { name: _pendingNewItemName });
  closeNewItemModal();
  await loadItems();
  const fresh = state.items.find((i) => i._id === item._id) || item;
  selectFoodItem(fresh, false);
});
$("#newitem-parent").addEventListener("click", async () => {
  const item = await apiJson("/api/items", "POST", { name: _pendingNewItemName });
  closeNewItemModal();
  await loadItems();
  const fresh = state.items.find((i) => i._id === item._id) || item;
  openSuboptionModal(fresh, { alreadyAddedParent: false });
});

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
  const q = $("#suboption-input").value;
  const candidates = filterByName(childrenOf(ctx.parentItem._id).length && !q
    ? childrenOf(ctx.parentItem._id) : state.items.filter((i) => i._id !== ctx.parentItem._id), q);
  candidates.__all = state.items;
  const list = $("#suboption-list");
  list.innerHTML = "";
  for (const it of candidates) {
    const chip = document.createElement("button");
    chip.type = "button";
    const ticked = ctx.ticked.has(it._id);
    chip.className = "chip" + (ticked ? " new" : "");
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
      const created = await apiJson("/api/items", "POST", { name: q.trim() });
      await apiJson("/api/item-links", "POST", { child: created._id, parent: ctx.parentItem._id });
      await loadItems();
      ctx.ticked.set(created._id, created.name);
      $("#suboption-input").value = "";
      renderSuboptionList();
    });
    list.appendChild(add);
  }
}
$("#suboption-input").addEventListener("input", renderSuboptionList);
$("#suboption-skip").addEventListener("click", async () => {
  const ctx = state.subOptionCtx;
  if (!ctx.alreadyAddedParent) addToDraft(ctx.parentItem);
  closeSuboptionModal();
});
$("#suboption-done").addEventListener("click", async () => {
  const ctx = state.subOptionCtx;
  if (!ctx.alreadyAddedParent) addToDraft(ctx.parentItem);
  for (const [id, name] of ctx.ticked) {
    const existingLink = state.itemLinks.some((l) => l.child === id && l.parent === ctx.parentItem._id);
    if (!existingLink) {
      await apiJson("/api/item-links", "POST", { child: id, parent: ctx.parentItem._id });
    }
    addToDraft({ _id: id, name });
  }
  await loadItems();
  closeSuboptionModal();
});

// ── holidays ─────────────────────────────────────────────────────────────
async function loadHolidays() {
  const rows = await api(`/api/holidays?frm=${daysAgoIso(14).slice(0, 10)}`);
  const ul = $("#holiday-list");
  ul.innerHTML = "";
  for (const h of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<div>${escapeHtml(h.name || h.location)}<div class="meta">${h.start} → ${h.stop} · ${escapeHtml(h.location)}</div></div>`;
    if (state.principal.role === "owner") {
      const del = document.createElement("button");
      del.className = "small ghost"; del.textContent = "🗑";
      del.addEventListener("click", async () => {
        if (!confirm("Delete this holiday?")) return;
        await api(`/api/holidays/${h._id}`, { method: "DELETE" });
        await loadHolidays(); await loadTimeline();
      });
      li.appendChild(del);
    }
    ul.appendChild(li);
  }
  return rows;
}
$("#add-holiday").addEventListener("click", async () => {
  const start = $("#holiday-start").value, stop = $("#holiday-stop").value;
  if (!start || !stop || !$("#holiday-location").value) { toast("Start, stop, and location are required", "error"); return; }
  await apiJson("/api/holidays", "POST", {
    start, stop, location: $("#holiday-location").value, name: $("#holiday-name").value || null,
  });
  $("#holiday-start").value = ""; $("#holiday-stop").value = "";
  $("#holiday-location").value = ""; $("#holiday-name").value = "";
  toast("Holiday added");
  await loadHolidays(); await loadTimeline();
});

// ── daily routine ────────────────────────────────────────────────────────
const ENERGY_LABELS = ["Very low", "Low", "Normal", "Good", "Great"];
function setTempDisplay() {
  const v = $("#temp-input").value;
  const disp = $("#temp-display");
  disp.innerHTML = v ? `${v}<span class="unit">°C</span>` : `——<span class="unit">°C</span>`;
  disp.classList.toggle("fever", v && parseFloat(v) >= (state.config?.fever_threshold ?? 37.8));
}
$("#temp-input").addEventListener("input", setTempDisplay);
$("#temp-minus").addEventListener("click", () => {
  $("#temp-input").value = (parseFloat($("#temp-input").value || "37") - 0.1).toFixed(1);
  setTempDisplay();
});
$("#temp-plus").addEventListener("click", () => {
  $("#temp-input").value = (parseFloat($("#temp-input").value || "37") + 0.1).toFixed(1);
  setTempDisplay();
});
$("#energy-range").addEventListener("input", () => {
  $("#energy-label").textContent = ENERGY_LABELS[$("#energy-range").value];
});

// pain body map
const PAIN_CYCLE = [null, "yellow", "orange", "red"];
function applyPainRegionVisual(region) {
  const sev = state.painMap[region] || "";
  $$(`.pain-region[data-region="${region}"]`).forEach((el) => {
    if (sev) el.setAttribute("data-severity", sev); else el.removeAttribute("data-severity");
  });
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
$("#body-tab-front").addEventListener("click", () => {
  $("#body-front").classList.remove("hidden"); $("#body-back").classList.add("hidden");
  $("#body-tab-front").classList.add("active"); $("#body-tab-front").classList.remove("ghost");
  $("#body-tab-back").classList.remove("active"); $("#body-tab-back").classList.add("ghost");
});
$("#body-tab-back").addEventListener("click", () => {
  $("#body-back").classList.remove("hidden"); $("#body-front").classList.add("hidden");
  $("#body-tab-back").classList.add("active"); $("#body-tab-back").classList.remove("ghost");
  $("#body-tab-front").classList.remove("active"); $("#body-tab-front").classList.add("ghost");
});

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
    onTap: (it) => { state.selectedSymptoms.set(it._id, it.name); $("#symptom-input").value = ""; renderSymptomTypeahead(); renderSelectedSymptoms(); },
    onAddNew: async (name) => {
      const created = await apiJson("/api/symptom-items", "POST", { name });
      await loadSymptomItems();
      state.selectedSymptoms.set(created._id, created.name);
      $("#symptom-input").value = "";
      renderSymptomTypeahead(); renderSelectedSymptoms();
    },
  });
}
$("#symptom-input").addEventListener("input", renderSymptomTypeahead);
function renderSelectedSymptoms() {
  const wrap = $("#symptom-selected");
  wrap.innerHTML = "";
  for (const [id, name] of state.selectedSymptoms) {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = name + " ✕";
    chip.addEventListener("click", () => { state.selectedSymptoms.delete(id); renderSelectedSymptoms(); });
    wrap.appendChild(chip);
  }
}

async function loadTodayHealth() {
  const today = new Date().toISOString().slice(0, 10);
  $("#routine-event-at").value = nowLocalInput();
  const rows = await api(`/api/health?date=${today}`);
  if (!rows.length) return;
  const h = rows[0];
  if (h.event_at) $("#routine-event-at").value = isoToLocalInput(h.event_at);
  if (h.temperature_celsius != null) { $("#temp-input").value = h.temperature_celsius; setTempDisplay(); }
  if (h.energy != null) { $("#energy-range").value = h.energy; $("#energy-label").textContent = ENERGY_LABELS[h.energy]; }
  if (h.sleep_hours != null) $("#sleep-input").value = h.sleep_hours;
  if (h.pain_scale != null) $("#pain-scale").value = h.pain_scale;
  if (h.pain_map) { state.painMap = { ...h.pain_map }; Object.keys(state.painMap).forEach(applyPainRegionVisual); }
  if (h.notes) $("#routine-notes").value = h.notes;
  for (const s of h.symptoms || []) {
    const item = state.symptomItems.find((i) => i._id === s.symptom_id);
    state.selectedSymptoms.set(s.symptom_id, item ? item.name : s.symptom_id);
  }
  renderSelectedSymptoms();
}

$("#save-routine").addEventListener("click", async () => {
  await apiJson("/api/health", "POST", {
    event_at: toIso($("#routine-event-at").value),
    temperature_celsius: $("#temp-input").value ? parseFloat($("#temp-input").value) : null,
    energy: parseInt($("#energy-range").value, 10),
    sleep_hours: $("#sleep-input").value ? parseFloat($("#sleep-input").value) : null,
    symptom_ids: Array.from(state.selectedSymptoms.keys()),
    pain_map: state.painMap,
    pain_scale: $("#pain-scale").value ? parseInt($("#pain-scale").value, 10) : null,
    notes: $("#routine-notes").value || null,
  });
  toast("Today's routine saved");
  await loadTimeline();
});

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
    onTap: (it) => { state.selectedBathroomKind = it; $("#bathroom-selected").textContent = it.name; $("#bathroom-input").value = ""; renderBathroomTypeahead(); },
    onAddNew: async (name) => {
      const created = await apiJson("/api/bathroom-items", "POST", { name });
      await loadBathroomItems();
      state.selectedBathroomKind = created;
      $("#bathroom-selected").textContent = created.name;
      $("#bathroom-input").value = "";
      renderBathroomTypeahead();
    },
  });
}
$("#bathroom-input").addEventListener("input", renderBathroomTypeahead);
$("#be-event-at").value = nowLocalInput();

$("#log-be").addEventListener("click", async () => {
  if (!state.selectedBathroomKind) { toast("Pick or type a kind first", "error"); return; }
  const fd = new FormData();
  fd.set("kind", state.selectedBathroomKind._id);
  if ($("#be-event-at").value) fd.set("event_at", toIso($("#be-event-at").value));
  fd.set("notes", $("#be-notes").value || "");
  const file = $("#be-photo").files[0];
  if (file) fd.set("photo", file);
  const res = await fetch("/api/bathroom-events", { method: "POST", credentials: "include", body: fd });
  if (!res.ok) { const d = await res.json().catch(() => ({})); toast(d.detail || "Failed to log event", "error"); return; }
  toast("Bathroom event logged");
  state.selectedBathroomKind = null;
  $("#bathroom-selected").textContent = "none";
  $("#be-notes").value = ""; $("#be-photo").value = ""; $("#be-event-at").value = nowLocalInput();
  await loadTimeline();
});

// ── notes ────────────────────────────────────────────────────────────────
$("#add-note").addEventListener("click", async () => {
  const text = $("#note-input").value.trim();
  if (!text) return;
  await apiJson("/api/notes", "POST", { text, event_at: toIso($("#note-event-at").value) });
  $("#note-input").value = "";
  toast("Note added");
  await loadTimeline();
});
$("#note-event-at").value = nowLocalInput();

// ── timeline ─────────────────────────────────────────────────────────────
async function loadTimeline() {
  const frm = daysAgoIso(14);
  const data = await api(`/api/timeline?frm=${encodeURIComponent(frm)}`);
  const bandsEl = $("#holiday-bands");
  bandsEl.innerHTML = "";
  for (const h of data.holidays || []) {
    const band = document.createElement("div");
    band.className = "holiday-band";
    band.textContent = `🗓️ ${h.name || "Holiday"}: ${h.start} → ${h.stop} · ${h.location}`;
    bandsEl.appendChild(band);
  }
  const list = $("#timeline-list");
  list.innerHTML = "";
  for (const entry of data.items || []) {
    list.appendChild(renderTimelineItem(entry));
  }
}
function renderTimelineItem(entry) {
  const div = document.createElement("div");
  div.className = "timeline-item " + entry.type;
  const time = document.createElement("div");
  time.className = "time"; time.textContent = fmtTime(entry.event_at);
  const body = document.createElement("div");
  body.className = "body";
  const actions = document.createElement("div");
  actions.className = "actions";

  if (entry.type === "meal") {
    const lines = entry.doc.lines;
    const names = lines.map((l) => (state.items.find((i) => i._id === l.food_id)?.name) || l.food_id).join(", ");
    body.textContent = `🍽️ ${names} (${lines[0].where}${lines[0].where_name ? " @ " + lines[0].where_name : ""})`;
    actions.appendChild(editTimeButton(entry.event_at, async (iso) => {
      await apiJson(`/api/intake/group/${entry.doc.intake_id}`, "PATCH", { consumed_at: iso });
      await loadTimeline();
    }));
    if (state.principal.role === "owner") {
      actions.appendChild(deleteButton(async () => {
        await api(`/api/intake/group/${entry.doc.intake_id}`, { method: "DELETE" }); await loadTimeline();
      }));
    }
  } else if (entry.type === "routine") {
    const h = entry.doc;
    const symptoms = (h.symptoms || []).map((s) => state.symptomItems.find((i) => i._id === s.symptom_id)?.name || s.symptom_id).join(", ");
    body.textContent = `🩺 temp ${h.temperature_celsius ?? "—"}°C · energy ${h.energy ?? "—"} · sleep ${h.sleep_hours ?? "—"}h` +
      (symptoms ? ` · symptoms: ${symptoms}` : "") + (h.pain_scale != null ? ` · pain ${h.pain_scale}/10` : "");
    actions.appendChild(editTimeButton(entry.event_at, async (iso) => {
      await apiJson(`/api/health/${h._id}`, "PATCH", { event_at: iso }); await loadTimeline();
    }));
  } else if (entry.type === "bathroom") {
    const b = entry.doc;
    const kindName = state.bathroomItems.find((i) => i._id === b.kind)?.name || b.kind;
    body.innerHTML = `🚻 ${escapeHtml(kindName)}${b.notes ? " — " + escapeHtml(b.notes) : ""}`;
    if (b.photo) {
      const img = document.createElement("img");
      img.className = "photo-thumb";
      loadPhotoBlobUrl(`/api/bathroom-events/${b._id}/photo`).then((url) => { if (url) img.src = url; });
      body.appendChild(img);
    }
    actions.appendChild(editTimeButton(entry.event_at, async (iso) => {
      const fd = new FormData(); fd.set("event_at", iso);
      await fetch(`/api/bathroom-events/${b._id}`, { method: "PATCH", credentials: "include", body: fd });
      await loadTimeline();
    }));
    if (state.principal.role === "owner") {
      actions.appendChild(deleteButton(async () => {
        await api(`/api/bathroom-events/${b._id}`, { method: "DELETE" }); await loadTimeline();
      }));
    }
  } else if (entry.type === "note") {
    const n = entry.doc;
    body.textContent = `📝 ${n.text}`;
    actions.appendChild(editTimeButton(entry.event_at, async (iso) => {
      await apiJson(`/api/notes/${n._id}`, "PATCH", { event_at: iso }); await loadTimeline();
    }));
    if (state.principal.role === "owner") {
      actions.appendChild(deleteButton(async () => {
        await api(`/api/notes/${n._id}`, { method: "DELETE" }); await loadTimeline();
      }));
    }
  }
  const row = document.createElement("div");
  row.style.display = "flex"; row.style.justifyContent = "space-between"; row.style.alignItems = "center"; row.style.gap = "8px";
  row.appendChild(body); row.appendChild(actions);
  div.appendChild(time); div.appendChild(row);
  return div;
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
function deleteButton(onClick) {
  const del = document.createElement("button");
  del.className = "small ghost"; del.textContent = "🗑";
  del.addEventListener("click", async () => { if (confirm("Delete this?")) await onClick(); });
  return del;
}

// ── ask ──────────────────────────────────────────────────────────────────
async function runAsk(save) {
  const question = $("#ask-question").value.trim();
  if (!question) { toast("Type a question first", "error"); return; }
  const res = await apiJson("/api/ask", "POST", {
    question,
    frm: $("#ask-from").value ? toIso($("#ask-from").value + "T00:00") : null,
    to: $("#ask-to").value ? toIso($("#ask-to").value + "T23:59") : null,
    save,
  });
  $("#ask-result").style.display = "block";
  $("#ask-result").textContent = res.context;
  if (save) { toast("Saved to Findings"); await loadFindings(); }
}
$("#ask-btn").addEventListener("click", () => runAsk(false));
$("#ask-save-btn").addEventListener("click", () => runAsk(true));

// ── findings ─────────────────────────────────────────────────────────────
async function loadFindings() {
  const rows = await api("/api/findings");
  const ul = $("#findings-list");
  ul.innerHTML = "";
  for (const f of rows) {
    const li = document.createElement("li");
    const title = f.title || f.question || "(untitled)";
    li.innerHTML = `<div><div>${escapeHtml(title)}</div>
      <div class="meta">${f.created_at ? fmtTime(f.created_at) : ""} · ${escapeHtml(f.confidence || "")} · ${escapeHtml(f.author || "")}</div>
      ${(f.findings || []).map((x) => `<div>• ${escapeHtml(x)}</div>`).join("")}
      ${f.answer ? `<div>${escapeHtml(f.answer)}</div>` : ""}</div>`;
    ul.appendChild(li);
  }
}

// ── boot ─────────────────────────────────────────────────────────────────
async function bootApp() {
  showPage("home");
  bindPainRegions();
  setTempDisplay();
  $("#energy-label").textContent = ENERGY_LABELS[$("#energy-range").value];
  $("#intake-consumed-at").value = nowLocalInput();
  await Promise.all([loadItems(), loadSymptomItems(), loadBathroomItems()]);
  await Promise.all([loadIntakeLog(), loadHolidays(), loadTodayHealth(), loadTimeline(), loadFindings()]);
  await loadSecuritySection();
}

applyStoredTheme();
tryAuthAndEnter();
