// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
"use strict";
// Panel de app-integration-icloud-sync. JS sin frameworks ni build: consulta /api/* y pinta.

// --- textos: nombres internos -> castellano, en un solo sitio -------------------------------------
const ACTIONS = {
  download:      { icon: "↓", done: "Descargado de iCloud",            doing: "Descargando",  group: "down" },
  upload:        { icon: "↑", done: "Subido a iCloud",                 doing: "Subiendo",     group: "up" },
  replace:       { icon: "↑", done: "Actualizado en iCloud",           doing: "Actualizando", group: "up" },
  conflict:      { icon: "⚠", done: "Conflicto: guardadas las dos versiones", doing: "Resolviendo conflicto", group: "down" },
  mkdir_local:   { icon: "📁", done: "Carpeta creada en el NAS",        doing: "Creando carpeta", group: "down" },
  mkdir_remote:  { icon: "📁", done: "Carpeta creada en iCloud",        doing: "Creando carpeta", group: "up" },
  delete_local:  { icon: "🗑", done: "Movido a la papelera del NAS",     doing: "Borrando",     group: "del" },
  delete_remote: { icon: "🗑", done: "Movido a Eliminados de iCloud",    doing: "Borrando",     group: "del" },
  rmdir_local:   { icon: "🗑", done: "Carpeta borrada del NAS",          doing: "Borrando carpeta", group: "del" },
  rmdir_remote:  { icon: "🗑", done: "Carpeta borrada de iCloud",        doing: "Borrando carpeta", group: "del" },
  rename_local:  { icon: "✎", done: "Renombrado/movido en el NAS (como en iCloud)", doing: "Renombrando", group: "down" },
  rename_remote: { icon: "✎", done: "Renombrado/movido en iCloud (como en el NAS)", doing: "Renombrando", group: "up" },
  rename_both:   { icon: "✎", done: "Renombrado en iCloud y en el NAS", doing: "Renombrando", group: "up" },
  // Copia de las apps (solo iCloud → NAS)
  apps_download: { icon: "↓", done: "Copiado de la app",                doing: "Copiando",     group: "down" },
  apps_replace:  { icon: "↓", done: "Actualizado desde la app (la versión anterior, a la papelera)", doing: "Actualizando", group: "down" },
  apps_mkdir:    { icon: "📁", done: "Carpeta de app creada en el NAS",  doing: "Creando carpeta", group: "down" },
  apps_trash:    { icon: "🗑", done: "Ya no está en la app: a la papelera del NAS", doing: "Retirando", group: "del" },
  apps_rmdir:    { icon: "🗑", done: "Carpeta de app retirada del NAS",  doing: "Retirando carpeta", group: "del" },
  heic_jpg:      { icon: "🖼", done: "JPG creado a partir del HEIC (se sube en la próxima sync)", doing: "Creando JPG de", group: "up" },
};
const PHASES = [
  ["connecting", "Conectando"], ["listing_remote", "Revisando iCloud"], ["scanning_local", "Revisando el NAS"],
  ["planning", "Preparando"], ["transferring", "Transfiriendo"], ["apps", "Copiando apps"], ["heic", "Creando JPG"],
];
const RUN_STATUS = {
  ok: ["Correcta", "ok"], partial: ["Con errores", "warn"], stopped: ["Parada", "warn"],
  skipped: ["En pausa", "warn"], aborted: ["Abortada", "bad"], error: ["Fallida", "bad"],
  interrupted: ["Cortada por reinicio", "warn"],
  pending: ["Esperando tu confirmación", "warn"],
};
const COUNTER_ORDER = [
  ["↓", ["download", "apps_download", "apps_replace"]], ["↑", ["upload", "replace"]],
  ["📁", ["mkdir_local", "mkdir_remote", "apps_mkdir"]],
  ["🗑", ["delete_local", "delete_remote", "rmdir_local", "rmdir_remote", "apps_trash", "apps_rmdir"]], ["⚠", ["conflict"]],
  ["🖼", ["heic_jpg"]],
];

// --- utilidades ----------------------------------------------------------------------------------
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nf = new Intl.NumberFormat(i18n.locale);

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toLocaleString(i18n.locale, { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}
function fmtDuration(s) {
  if (s == null) return "";
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min` : `${m} min ${s % 60 ? (s % 60) + " s" : ""}`.trim();
}
function toDate(v) { return v == null ? null : typeof v === "number" ? new Date(v * 1000) : new Date(v); }
function fmtAbs(v) {
  const d = toDate(v);
  return d ? d.toLocaleString(i18n.locale, { dateStyle: "short", timeStyle: "short" }) : "";
}
function fmtRel(v) {
  const d = toDate(v);
  if (!d) return "";
  const diff = (d - Date.now()) / 1000, a = Math.abs(diff);
  const txt = a < 45 ? "unos segundos" : a < 3600 ? `${Math.round(a / 60)} min`
    : a < 86400 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} d`;
  return diff > 0 ? `en ${txt}` : `hace ${txt}`;
}
function splitPath(p) {
  const i = p.lastIndexOf("/");
  return i < 0 ? ["", p] : [p.slice(0, i + 1), p.slice(i + 1)];
}
function pathHtml(p) {
  const [dir, name] = splitPath(p);
  return `<span class="dir">${esc(dir)}</span>${esc(name)}`;
}
function countsSummary(counts) {
  const parts = [];
  for (const [icon, kinds] of COUNTER_ORDER) {
    const n = kinds.reduce((a, k) => a + (counts[k] || 0), 0);
    if (n) parts.push(`${icon} ${nf.format(n)}`);
  }
  return parts.join(" · ");
}

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

let offline = false;
async function api(path, opts = {}) {
  try {
    const r = await fetch(path, {
      ...opts, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch", ...(opts.headers || {}) },
    });
    if (r.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname)}`); throw new Error("Sesión caducada"); }
    if (offline) { offline = false; $("offline").classList.add("hidden"); }
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(body.detail || r.statusText), { http: r.status });
    return body;
  } catch (e) {
    if (e.http === undefined) { offline = true; $("offline").classList.remove("hidden"); }
    throw e;
  }
}
const post = (path, body) => api(path, { method: "POST", body: body ? JSON.stringify(body) : null });

// --- estado global -------------------------------------------------------------------------------
let P = null;            // último /api/progress
let S = null;            // último /api/status
let feed = [];           // actividad acumulada (últimas 300)
let feedSeq = 0;
let feedFilter = "all";
let logSeq = 0;
let logRunStart = null;
let wasRunning = false;

// --- cabecera de estado --------------------------------------------------------------------------
function renderStatus() {
  if (!P) return;
  const last = S?.runs?.[0];
  let dot = "", title = "", detail = "";
  if (P.auth_state === "needs_2fa") { dot = "warn"; title = "Necesita tu código"; detail = "La sincronización está en pausa hasta que introduzcas el código de Apple."; }
  else if (P.auth_state === "not_configured") { dot = "bad"; title = "Sin configurar"; detail = P.auth_message; }
  else if (P.auth_state === "error") { dot = "bad"; title = "Error con la cuenta de Apple"; detail = P.auth_message; }
  else if (P.running) { dot = "run"; title = P.dry_run ? "Simulando…" : "Sincronizando…"; }
  else if (P.pending_deletes) { dot = "warn"; title = "Esperando tu confirmación"; detail = "Hay borrados retenidos: revísalos abajo. El resto se sincroniza con normalidad."; }
  else if (P.failing_since) { dot = "bad"; title = "La sincronización está fallando"; detail = last?.message || ""; }
  else if (P.auth_state === "disconnected") { dot = "warn"; title = "Reconectando con Apple"; detail = P.auth_message; }
  else if (!last) { dot = ""; title = "Esperando la primera sincronización"; }
  else if (last.status === "ok") { dot = "ok"; title = last.dry_run ? "Simulación terminada" : "Todo sincronizado"; }
  else if (last.status === "partial") { dot = "warn"; title = "Sincronizado con errores"; detail = `${last.errors.filter(e => !e.startsWith("aviso:")).length} fichero(s) no se pudieron sincronizar; se reintentan en la próxima.`; }
  else if (last.status === "stopped") { dot = "warn"; title = "Sincronización parada"; detail = "La próxima continúa donde se quedó."; }
  else { const [txt, cls] = RUN_STATUS[last.status] || [last.status, "bad"]; dot = cls; title = `Última sincronización: ${txt.toLowerCase()}`; detail = last.message; }

  $("status-dot").className = "dot " + dot;
  $("status-title").textContent = title;
  $("status-detail").textContent = detail || "";

  const sub = [];
  if (P.running && P.elapsed_s != null) sub.push(`en marcha desde hace ${fmtDuration(P.elapsed_s)}`);
  else if (last?.finished_at) sub.push(`última ${fmtRel(last.finished_at)}`);
  if (!P.running && P.auth_state === "ok") sub.push(`próxima ${fmtRel(P.next_run_at)}`);
  $("status-sub").textContent = sub.join(" · ");
  $("status-sub").title = last?.finished_at ? `Última: ${fmtAbs(last.finished_at)}\nPróxima: ${fmtAbs(P.next_run_at)}` : "";

  const chips = [];
  if (S?.config?.dry_run) chips.push(`<span class="chip warn">Modo simulación: no cambia nada</span>`);
  if (P.forced_dry_run) chips.push(`<span class="chip bad">Simulación forzada (fichero data/SIMULACION): no cambia nada</span>`);
  if (S?.open_conflicts) chips.push(`<span class="chip warn">⚠ ${S.open_conflicts} conflicto(s) por revisar</span>`);
  if (S?.config?.icloud_user) chips.push(`<span class="chip">${esc(S.config.icloud_user)}</span>`);
  if (S?.snapshot_entries) chips.push(`<span class="chip">${_t("{n} elementos sincronizados", { n: nf.format(S.snapshot_entries) })}</span>`);
  $("status-chips").innerHTML = chips.join("");

  $("btn-run").classList.toggle("hidden", P.running);
  $("btn-dry").classList.toggle("hidden", P.running);
  $("btn-run").disabled = $("btn-dry").disabled = P.auth_state !== "ok";
  $("btn-stop").classList.toggle("hidden", !P.running);
  $("btn-stop").disabled = P.stop_requested;
  $("btn-stop").textContent = P.stop_requested ? "Parando…" : "Parar";
}

// --- sesión de Apple (común a Archivos y Fotos) ----------------------------------------------------
function renderSession() {
  const st = P?.auth_state;
  const show = ["error", "disconnected", "not_configured"].includes(st);
  $("session").classList.toggle("hidden", !show);
  if (!show) return;
  $("session-title").textContent = st === "disconnected" ? "Reconectando con Apple" : st === "not_configured"
    ? "Falta configurar la cuenta de Apple" : "Error con la cuenta de Apple";
  $("session-msg").textContent = P.auth_message || "";
  $("btn-relogin").classList.toggle("hidden", st === "not_configured" || P.running);
}

// --- pestañas: Archivos (Drive), Fotos y Mi iCloud --------------------------------------------------
const TABS = ["archivos", "fotos", "icloud"];
let currentTab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1)
  : (TABS.includes(localStorage.getItem("tab")) ? localStorage.getItem("tab") : "archivos");

function showTab(name) {
  currentTab = name;
  localStorage.setItem("tab", name);
  history.replaceState(null, "", "#" + name);
  for (const t of TABS) $("tab-" + t).classList.toggle("hidden", t !== name);
  for (const b of $("tabs").children) b.setAttribute("aria-selected", b.dataset.tab === name);
  if (name === "fotos" && typeof kickPhotos === "function") kickPhotos();
  if (name === "icloud") { icloudLoadedFor = null; maybeLoadIcloud(); }
}

// Punto en cada pestaña: azul = en marcha, ámbar = algo que revisar, rojo = fallando
function renderTabDots() {
  const dot = (running, attention, failing) => failing ? "bad" : attention ? "warn" : running ? "run" : "";
  $("tabdot-archivos").className = "tabdot " + dot(P?.running, P?.pending_deletes || S?.open_conflicts, P?.failing_since);
  const ph = typeof PH === "undefined" ? null : PH;
  $("tabdot-fotos").className = "tabdot " + dot(ph?.running, ph?.pending_deletes, ph?.failing_since);
}

// --- 2FA -----------------------------------------------------------------------------------------
let twofaShown = false;
function renderTwofa() {
  const show = P?.auth_state === "needs_2fa";
  $("twofa").classList.toggle("hidden", !show);
  if (!show) { twofaShown = false; return; }
  const via = P.two_factor_delivery;
  $("twofa-hint").textContent = via === "sms"
    ? "Apple te ha enviado un SMS con un código de 6 dígitos."
    : via === "trusted_device"
      ? "Mira tu iPhone, iPad o Mac: Apple muestra allí un código de 6 dígitos (pulsa «Permitir» si pregunta)."
      : "Introduce el código de verificación de 6 dígitos que te ha enviado Apple.";
  if (!twofaShown) { twofaShown = true; $("twofa-code").focus(); }
}

let verifying = false;
async function submitCode(ev) {
  ev?.preventDefault();
  const code = $("twofa-code").value.replace(/\D/g, "");
  if (verifying || code.length < 6) return;
  verifying = true;
  const btn = $("twofa-submit"), msg = $("twofa-msg");
  btn.disabled = true; btn.textContent = "Verificando…";
  msg.className = "form-msg"; msg.textContent = "";
  try {
    const r = await post("/api/auth/2fa", { code });
    msg.className = "form-msg " + (r.ok ? "ok" : "bad");
    msg.textContent = r.ok ? "✓ Código correcto. Empezando a sincronizar…" : r.message;
    if (r.ok) { $("twofa-code").value = ""; toast("Sesión de Apple verificada"); }
  } catch (e) {
    msg.className = "form-msg bad"; msg.textContent = `No se pudo verificar: ${e.message}`;
  } finally {
    verifying = false; btn.disabled = false; btn.textContent = "Verificar";
    kickProgress();
  }
}

async function newCode() {
  $("twofa-code").value = "";
  $("twofa-msg").className = "form-msg";
  $("twofa-msg").textContent = "Pidiendo un código nuevo a Apple…";
  try {
    const r = await post("/api/auth/relogin");
    $("twofa-msg").textContent = r.ok ? "Código nuevo enviado. Usa solo este." : r.message;
  } catch (e) { $("twofa-msg").className = "form-msg bad"; $("twofa-msg").textContent = e.message; }
  $("twofa-code").focus();
  kickProgress();
}

// --- progreso ------------------------------------------------------------------------------------
function renderProgress() {
  const card = $("progress");
  if (!P?.running) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("progress-title").textContent = P.dry_run ? "Simulando (no se cambia nada)" : "Sincronizando";
  $("progress-elapsed").textContent = P.elapsed_s != null ? fmtDuration(P.elapsed_s) : "";

  const idx = PHASES.findIndex(([k]) => k === P.phase);
  $("steps").innerHTML = PHASES.map(([k, label], i) =>
    `<li class="${i < idx ? "done" : i === idx ? "now" : ""}">${label}</li>`).join("");

  let line = "";
  if (P.phase === "connecting") line = "Conectando con iCloud…";
  else if (P.phase === "listing_remote") line = `${nf.format(P.folders_scanned)} carpetas · ${nf.format(P.files_seen)} elementos vistos` +
    (P.current_folder ? ` · ahora: <b>${esc(P.current_folder)}</b>` : "");
  else if (P.phase === "scanning_local") line = "Comparando con la carpeta del NAS…";
  else if (P.phase === "planning") line = "Calculando qué hay que copiar o borrar…";
  else if (P.phase === "transferring" && !P.actions_total) line = "No hay nada que cambiar.";
  else if (P.phase === "apps" && !P.actions_total) line = "Revisando las apps (Pages, Numbers…): solo se copian de iCloud al NAS.";
  $("progress-line").innerHTML = line;

  const transferring = ["transferring", "apps", "heic"].includes(P.phase) && P.actions_total > 0;
  $("transfer").classList.toggle("hidden", !transferring);
  if (!transferring) return;

  const pa = Math.min(100, 100 * P.actions_done / P.actions_total);
  const pb = P.bytes_total ? Math.min(100, 100 * P.bytes_done / P.bytes_total) : pa;
  $("bar-actions").style.width = pa + "%";
  $("bar-bytes").style.width = pb + "%";
  $("legend-actions").textContent = `${nf.format(P.actions_done)} de ${nf.format(P.actions_total)} operaciones`;
  $("legend-bytes").textContent = P.bytes_total ? `${_t("{a} de {b}", { a: fmtBytes(P.bytes_done), b: fmtBytes(P.bytes_total) })}` : "";
  $("legend-speed").textContent = P.speed_bps ? `${fmtBytes(P.speed_bps)}/s` : "";
  $("legend-eta").textContent = P.eta_s != null ? `quedan ~${fmtDuration(P.eta_s)}` : "";

  const c = P.current;
  if (c) {
    const a = ACTIONS[c.kind] || { icon: "•", doing: c.kind };
    const pc = c.size ? Math.min(100, 100 * c.done / c.size) : 0;
    $("current").innerHTML = `${a.icon} ${esc(_t(a.doing))} ${pathHtml(c.path)}` +
      (c.size ? ` <span class="muted">(${fmtBytes(c.size)})</span><div class="bar"><div style="width:${pc}%"></div></div>` : "");
  } else $("current").innerHTML = "";

  const counters = [];
  for (const [icon, kinds] of COUNTER_ORDER) {
    const n = kinds.reduce((acc, k) => acc + (P.counts_done[k] || 0), 0);
    if (n) counters.push(`<span>${icon} ${nf.format(n)}</span>`);
  }
  if (P.errors) counters.push(`<span class="bad">✕ ${nf.format(P.errors)} con error</span>`);
  $("counters").innerHTML = counters.join("");
}

// --- actividad -----------------------------------------------------------------------------------
function renderFeed() {
  const items = feed.filter(a => feedFilter === "all" ? true
    : feedFilter === "err" ? !a.ok : a.ok && (ACTIONS[a.kind]?.group === feedFilter));
  $("feed").innerHTML = items.slice().reverse().slice(0, 200).map(a => {
    const d = ACTIONS[a.kind] || { icon: "•", done: a.kind };
    const meta = [d.done, a.size ? fmtBytes(a.size) : "", new Date(a.ts * 1000).toLocaleTimeString(i18n.locale)]
      .filter(Boolean).join(" · ");
    return `<li class="${a.ok ? "" : "err"}"><span class="icon">${a.ok ? d.icon : "✕"}</span>
      <div class="what"><div class="path">${pathHtml(a.path)}</div><div class="meta">${esc(meta)}</div>
      ${a.ok ? "" : `<div class="errmsg">${esc(a.error)}</div>`}</div></li>`;
  }).join("");
  $("feed-empty").classList.toggle("hidden", items.length > 0);
  $("feed-empty").textContent = feed.length ? "Nada con este filtro." : "Aún no hay actividad desde que arrancó la app.";
}

async function refreshFeed() {
  const items = await api(`/api/activity?since=${feedSeq}`);
  if (!items.length) return;
  feed.push(...items);
  if (feed.length > 300) feed = feed.slice(-300);
  feedSeq = items[items.length - 1].seq;
  renderFeed();
}

// --- historial, conflictos, config ---------------------------------------------------------------
function renderRuns() {
  const runs = S?.runs || [];
  $("runs").innerHTML = runs.length ? runs.map(r => {
    const [txt, cls] = r.finished_at ? (RUN_STATUS[r.status] || [r.status, ""]) : ["En curso", ""];
    const errs = (r.errors || []).filter(e => !e.startsWith("aviso:"));
    const warns = (r.errors || []).filter(e => e.startsWith("aviso:"));
    const bytes = [r.bytes_down ? _t("{b} descargados", { b: fmtBytes(r.bytes_down) }) : "", r.bytes_up ? _t("{b} subidos", { b: fmtBytes(r.bytes_up) }) : ""].filter(Boolean).join(", ");
    const summary = countsSummary(r.counts || {}) || (r.status === "ok" ? "Sin cambios" : r.message);
    const list = (arr, label) => arr.length ? `<details><summary>${arr.length} ${label}</summary><ul class="list">${arr.slice(0, 50).map(e => `<li>${esc(e.replace(/^aviso: /, ""))}</li>`).join("")}</ul></details>` : "";
    return `<li><span class="when" title="${esc(fmtAbs(r.started_at))}">${esc(fmtRel(r.started_at))}
        ${r.duration_s != null ? `<span class="muted small">· ${fmtDuration(r.duration_s)}</span>` : ""}</span>
      <span class="st ${cls}">${esc(txt)}${r.dry_run ? " · simulación" : ""}</span>
      <span class="summary">${esc(summary)} ${bytes ? `<span class="muted small">${esc(bytes)}</span>` : ""}
        ${r.status !== "ok" && r.message && summary !== r.message ? `<div class="muted small">${esc(r.message)}</div>` : ""}</span>
      ${list(errs, "errores")}${list(warns, "avisos (ficheros ignorados a propósito)")}</li>`;
  }).join("") : `<li class="muted">Todavía no hay ninguna sincronización.</li>`;
}

async function refreshConflicts() {
  const n = S?.open_conflicts || 0;
  $("conflicts-card").classList.toggle("hidden", !n);
  if (!n) return;
  const items = await api("/api/conflicts");
  $("conflicts-count").textContent = items.length;
  $("conflicts").innerHTML = items.map(c => `<li><div class="row"><div class="path">${pathHtml(c.path)}</div>
      <button data-resolve="${c.id}">Revisado</button></div>
      <div class="muted small">Copia del NAS: ${esc(c.conflict_copy)} · ${esc(fmtAbs(c.detected_at))}</div></li>`).join("");
}

function renderConfig() {
  const c = S?.config;
  if (!c) return;
  const rows = [
    ["Cuenta de Apple", c.icloud_user || "—"],
    ["Carpeta del NAS", c.sync_dir],
    ["Copia de las apps (solo iCloud → NAS)", c.apps_dir || "Desactivada (SYNC_APPS=false)"],
    ["JPG junto a cada HEIC", c.heic_to_jpg || "Desactivado (HEIC_TO_JPG=false)"],
    ["Qué se sincroniza", c.sync_folders.join(", ")],
    ["Cada", `${c.interval_min} min`],
    ["Modo", c.dry_run ? "Simulación (DRY_RUN=true): no cambia nada" : "Real"],
    ["Pedir confirmación si se borran más de", `${c.confirm_deletes_over} ficheros`],
    ["Límite de borrados de la copia de apps", c.max_delete],
    ["Papelera local", `${c.trash_days} días`],
    ["Avisos por email", c.email_notifications ? `Sí, a ${c.email_to}` : "No (faltan SMTP_USERNAME/SMTP_PASSWORD)"],
    ["Se ignoran", c.excludes.join("  ")],
  ];
  $("config").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  $("conflict-label").textContent = `(${c.conflict_name || "conflicto NAS"} …)`;
  $("footer").textContent = `app-integration-icloud-sync · versión ${S.version}`;
  $("drive-dir").textContent = c.sync_dir;
}

// --- borrados pendientes de confirmar -------------------------------------------------------------
let pendingBatch = null;
let pendingGroups = [];
let pendingLoadedFor = null;

async function loadPending() {
  const n = P?.pending_deletes || 0;
  $("pending").classList.toggle("hidden", !n);
  if (!n) { pendingBatch = null; pendingLoadedFor = null; return; }
  const key = P.pending_batch;
  if (pendingLoadedFor === key && pendingBatch) return;
  const s = await api("/api/pending-deletes");
  if (!s.batch_id) return;
  pendingBatch = s.batch_id; pendingLoadedFor = key;
  $("pending-text").textContent = s.text;
  $("pending-folders").innerHTML = s.folders.map((f, idx) => `<details class="pfolder">
      <summary>
        <span class="name"><b>${esc(f.folder || "/")}</b><br><span class="side">${esc(f.side_text)}</span></span>
        <span class="small">${nf.format(f.count)} fichero(s)${f.bytes ? ` · ${fmtBytes(f.bytes)}` : ""}</span>
        <span class="btns">
          <button class="danger" data-g="${idx}" data-d="confirm">Borrar</button>
          <button data-g="${idx}" data-d="reject">Restaurar</button>
        </span>
      </summary>
      <ul class="list small">${f.items.map(i => `<li><span class="icon">${i.kind.startsWith("rmdir") ? "📁" : "🗑"}</span>
        <div class="what"><div class="path">${pathHtml(i.path)}</div>
        ${i.size ? `<div class="meta">${fmtBytes(i.size)}</div>` : ""}</div></li>`).join("")}</ul>
    </details>`).join("");
  pendingGroups = s.folders;
}

async function decideGroup(idx, decision) {
  const g = pendingGroups[idx];
  if (!g || !pendingBatch) return;
  const where = g.side === "icloud" ? "en iCloud (a Eliminados recientemente, recuperable 30 días)" : "en el NAS (a su papelera)";
  const msg = decision === "confirm"
    ? `¿Borrar ${g.count} fichero(s) de «${g.folder || "/"}» también ${where}?`
    : `¿Restaurar ${g.count} fichero(s) de «${g.folder || "/"}»? Se volverán a traer desde el otro lado.`;
  if (!i18n.confirm(msg)) return;
  try {
    const r = await post(`/api/pending-deletes/${pendingBatch}/decide`, { folder: g.folder, side: g.side, decision });
    toast(r.message, !r.ok);
    pendingLoadedFor = null;
  } catch (e) { toast(e.message, true); }
  kickProgress(); kickStatus();
}

async function decidePending(decision) {
  if (!pendingBatch) return;
  const msg = decision === "confirm"
    ? "¿Borrar TODO el lote? Lo borrado en el NAS se borrará también en iCloud (a Eliminados recientemente, recuperable 30 días) y lo borrado en iCloud irá a la papelera del NAS."
    : "¿Restaurar TODO el lote? Se volverán a traer todos esos ficheros desde el lado donde siguen existiendo.";
  if (!i18n.confirm(msg)) return;
  for (const id of ["pending-confirm", "pending-reject"]) $(id).disabled = true;
  try {
    const r = await post(`/api/pending-deletes/${pendingBatch}/${decision}`);
    toast(r.message, !r.ok);
    pendingLoadedFor = null;
  } catch (e) { toast(e.message, true); }
  finally { for (const id of ["pending-confirm", "pending-reject"]) $(id).disabled = false; }
  kickProgress(); kickStatus();
}

// --- tu iCloud: almacenamiento y dispositivos -----------------------------------------------------
const DEVICE_ICONS = { iPhone: "📱", iPad: "📱", Mac: "💻", MacBook: "💻", iMac: "🖥", Watch: "⌚", AirPods: "🎧", iPod: "🎵" };
let icloudLoadedFor = null;   // estado de la sesión con el que se cargó por última vez

async function loadStorage(refresh = false) {
  try {
    const st = await api(`/api/account/storage${refresh ? "?refresh=true" : ""}`);
    $("storage-msg").classList.add("hidden");
    $("storage-body").classList.remove("hidden");
    $("storage-used").textContent = _t("{b} usados", { b: fmtBytes(st.used_bytes) });
    $("storage-total").textContent = `de ${fmtBytes(st.total_bytes)} · quedan ${fmtBytes(st.available_bytes)} (${st.used_percent.toLocaleString(i18n.locale)} % usado)`;
    const total = st.total_bytes || 1;
    $("storage-bar").className = "stack" + (st.over_quota || st.used_percent >= 95 ? " full" : "");
    $("storage-bar").innerHTML = st.by_media.filter(m => m.bytes > 0).map(m =>
      `<span style="width:${(100 * m.bytes / total).toFixed(2)}%;background:${esc(m.color)}" title="${esc(m.label)}: ${fmtBytes(m.bytes)}"></span>`).join("");
    $("storage-legend").innerHTML = st.by_media.filter(m => m.bytes > 0).map(m =>
      `<li><i style="background:${esc(m.color)}"></i>${esc(m.label)} <span class="muted">${fmtBytes(m.bytes)}</span></li>`).join("") +
      `<li><i style="background:var(--bg);outline:1px solid var(--line)"></i>Libre <span class="muted">${fmtBytes(st.available_bytes)}</span></li>`;
    if (st.over_quota) toast("Tu iCloud está lleno: iCloud Drive no aceptará subidas nuevas", true);
  } catch (e) {
    $("storage-body").classList.add("hidden");
    $("storage-msg").classList.remove("hidden");
    $("storage-msg").textContent = `No se pudo leer el almacenamiento: ${e.message}`;
  }
}

function deviceIcon(d) {
  const k = Object.keys(DEVICE_ICONS).find(k => (d.device_class || d.model).includes(k));
  return k ? DEVICE_ICONS[k] : "📟";
}

async function loadDevices(refresh = false) {
  try {
    const r = await api(`/api/devices${refresh ? "?refresh=true" : ""}`);
    const list = r.devices;
    const hiddenTxt = r.hidden ? `${r.hidden} dispositivo(s) oculto(s): se eligen en ${r.config_file}` : "";
    $("devices-msg").classList.toggle("hidden", list.length > 0 && !r.hidden);
    $("devices-msg").textContent = list.length ? hiddenTxt : `No hay dispositivos visibles. ${hiddenTxt}`;
    $("devices").innerHTML = list.map(d => {
      const battery = d.battery != null
        ? `<span class="${d.battery <= 20 ? "low" : ""}">🔋 ${d.battery} %${d.battery_status === "Charging" ? " ⚡" : ""}</span>` : "";
      const status = d.status === "online" ? `<span class="online">● conectado</span>` : `<span>○ ${d.status === "offline" ? "sin conexión" : "desconocido"}</span>`;
      return `<li><span class="icon">${deviceIcon(d)}</span>
        <div class="what"><div>${esc(d.name)}</div><div class="meta">${esc(d.model)} · ${status}${battery ? " · " + battery : ""}</div></div>
        <button data-sound="${esc(d.id)}" data-name="${esc(d.name)}" ${d.sound_available ? "" : "disabled title=\"Este dispositivo no admite sonido\""}>🔊 Hacer sonar</button></li>`;
    }).join("");
  } catch (e) {
    $("devices").innerHTML = "";
    $("devices-msg").classList.remove("hidden");
    $("devices-msg").textContent = `No se pudieron leer los dispositivos: ${e.message}`;
  }
}

// Se carga al abrir el panel y cada vez que la sesión pasa a estar activa (p.ej. tras el 2FA)
function maybeLoadIcloud() {
  if (currentTab !== "icloud") return;
  const state = P?.auth_state;
  if (state === icloudLoadedFor) return;
  icloudLoadedFor = state;
  if (state === "ok") { loadStorage(); loadDevices(); }
  else {
    $("storage-body").classList.add("hidden");
    for (const id of ["storage-msg", "devices-msg"]) {
      $(id).classList.remove("hidden");
      $(id).textContent = "Disponible cuando la sesión de Apple esté activa.";
    }
    $("devices").innerHTML = "";
  }
}

// --- log -----------------------------------------------------------------------------------------
async function refreshLog() {
  if (!$("log-details").open) return;
  if (P && P.started_at !== logRunStart) { logRunStart = P.started_at; logSeq = 0; $("log").textContent = ""; }
  const r = await api(`/api/log?since=${logSeq}`);
  const pre = $("log");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
  if (!r.live && logSeq === 0) pre.textContent = "";
  if (r.lines.length) pre.textContent += r.lines.join("\n") + "\n";
  if (!pre.textContent) pre.textContent = "Todavía no hay log.";
  logSeq = r.seq;
  $("log-state").textContent = r.live ? "en vivo" : "última sincronización";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

// --- bucles de refresco --------------------------------------------------------------------------
async function refreshProgress() {
  try {
    P = await api("/api/progress");
    // Al terminar un run, el historial y los conflictos cambian: se recargan ya.
    if (wasRunning && !P.running) await refreshStatus();
    wasRunning = P.running;
    renderStatus(); renderSession(); renderTwofa(); renderProgress(); renderTabDots(); maybeLoadIcloud();
    await loadPending();
    await refreshFeed();
    await refreshLog();
  } catch (e) { /* el banner de "sin conexión" ya avisa */ }
}

async function refreshStatus() {
  try {
    S = await api("/api/status");
    renderStatus(); renderRuns(); renderConfig();
    await refreshConflicts();
  } catch (e) { /* idem */ }
}

// Bucle de refresco con ritmo adaptable; kick() lo acelera en el acto (p.ej. al lanzar un run),
// sin esperar a que venza el temporizador lento.
function schedule(fn, fast, slow) {
  let timer = null, busy = false;
  const tick = async () => {
    clearTimeout(timer);
    if (busy) return;
    busy = true;
    try { await fn(); } finally { busy = false; }
    const active = P?.running || P?.auth_state === "needs_2fa";
    timer = setTimeout(tick, document.hidden ? slow * 3 : active ? fast : slow);
  };
  tick();
  return tick;
}

// --- eventos -------------------------------------------------------------------------------------
async function runNow(dry) {
  try {
    const r = await post(`/api/sync/run${dry ? "?dry_run=true" : ""}`);
    toast(r.message, !r.ok);
  } catch (e) { toast(e.message, true); }
  setTimeout(kickProgress, 500);
}

$("btn-run").onclick = () => runNow(false);
$("btn-dry").onclick = () => runNow(true);
$("btn-stop").onclick = async () => {
  if (!i18n.confirm("¿Parar la sincronización al terminar el fichero actual? La próxima continuará donde se quede.")) return;
  try { const r = await post("/api/sync/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  kickProgress();
};
$("pending-folders").onclick = e => {
  const b = e.target.closest("button[data-g]");
  if (!b) return;
  e.preventDefault();              // que el clic no abra/cierre el desplegable
  decideGroup(Number(b.dataset.g), b.dataset.d);
};
$("pending-confirm").onclick = () => decidePending("confirm");
$("pending-reject").onclick = () => decidePending("reject");
$("tabs").onclick = e => { const b = e.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); };
showTab(currentTab);
$("logout").onclick = async () => { await post("/auth/logout").catch(() => {}); location.replace("/login"); };
// Panel sin login (AUTH_DISABLED): sin enlaces de cuenta y con un aviso fijo
fetch("/auth/state").then(r => r.json()).then(s => {
  if (s.auth_disabled) { $("account-nav").classList.add("hidden"); $("auth-banner").classList.remove("hidden"); }
}).catch(() => {});
$("btn-relogin").onclick = async () => {
  try { const r = await post("/api/auth/relogin"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  kickProgress(); kickStatus();
};
$("icloud-refresh").onclick = async () => {
  const b = $("icloud-refresh");
  b.disabled = true; b.textContent = "Actualizando…";
  await Promise.all([loadStorage(true), loadDevices(true)]);
  b.disabled = false; b.textContent = "↻ Actualizar";
};
$("devices").onclick = async e => {
  const btn = e.target.closest("button[data-sound]");
  if (!btn) return;
  if (!i18n.confirm(`¿Hacer sonar «${btn.dataset.name}»? Sonará a todo volumen aunque esté en silencio, y Apple te enviará un email de aviso.`)) return;
  btn.disabled = true; const label = btn.textContent; btn.textContent = "Enviando…";
  try { const r = await post("/api/devices/play-sound", { device_id: btn.dataset.sound }); toast(r.message, !r.ok); }
  catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; btn.textContent = label; }
};
setInterval(() => { if (P?.auth_state === "ok" && !document.hidden && currentTab === "icloud") loadStorage(); }, 10 * 60 * 1000);
$("twofa-form").onsubmit = submitCode;
$("twofa-code").oninput = e => { if (e.target.value.replace(/\D/g, "").length === 6) submitCode(); };
$("twofa-new").onclick = newCode;
$("filters").onclick = e => {
  const f = e.target.dataset?.f;
  if (!f) return;
  feedFilter = f;
  for (const b of $("filters").children) b.classList.toggle("on", b.dataset.f === f);
  renderFeed();
};
$("conflicts").onclick = async e => {
  const id = e.target.dataset?.resolve;
  if (!id) return;
  try { const r = await post(`/api/conflicts/${id}/resolve`); toast(r.message); } catch (err) { toast(err.message, true); }
  refreshStatus();
};
$("log-details").addEventListener("toggle", () => { logSeq = 0; logRunStart = P?.started_at; $("log").textContent = ""; refreshLog(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { kickProgress(); kickStatus(); } });
setInterval(() => { if (P) renderStatus(); }, 30000);   // mantiene al día los "hace X min"

const kickStatus = schedule(refreshStatus, 10000, 30000);
const kickProgress = schedule(refreshProgress, 2000, 10000);
