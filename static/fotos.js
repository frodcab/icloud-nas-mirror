// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
"use strict";
// Pestaña "Fotos" del panel. Usa las utilidades de app.js ($, esc, nf, fmtBytes, api, post, P…).

const PH_PHASES = [
  ["scanning", "Revisando el NAS"], ["listing", "Preguntando a iCloud"], ["planning", "Preparando"],
  ["transferring", "Transfiriendo"],
];
const PH_COUNTS = [
  ["↓", ["descargas"]], ["↓ ediciones", ["descargas_editadas"]], ["↑", ["subidas", "colocadas"]],
  ["🗑 en iCloud", ["borradas_en_icloud"]], ["🗑 al NAS", ["a_papelera"]], ["🗑", ["borrados"]],
  ["✎", ["movidas_en_nas"]], ["🔗", ["enlazadas"]], ["= ya estaban", ["ya_estaban"]],
  ["✕ descartadas", ["descartadas"]],
];
const PH_ACTIONS = { download: "↓ Descargando", upload: "↑ Subiendo", delete: "🗑 Borrando" };
let PH = null;
let phWasRunning = false;
let phPendingBatch = null, phPendingGroups = [], phPendingFor = null;

function phCounts(counts) {
  const parts = [];
  for (const [label, kinds] of PH_COUNTS) {
    const n = kinds.reduce((a, k) => a + ((counts || {})[k] || 0), 0);
    if (n) parts.push(`${label} ${nf.format(n)}`);
  }
  return parts.join(" · ");
}

function renderPhotosStatus() {
  const last = PH.runs?.[0];
  const auth = P?.auth_state;
  let dot = "", title = "", detail = "";
  if (auth && auth !== "ok") { dot = "warn"; title = "En pausa"; detail = "Esperando a la sesión de Apple (aviso de arriba)."; }
  else if (PH.running) { dot = "run"; title = PH.progress?.dry_run ? "Simulando…" : "Sincronizando…"; }
  else if (PH.pending_deletes) { dot = "warn"; title = "Esperando tu confirmación"; detail = "Hay fotos borradas retenidas: revísalas abajo. El resto se sincroniza con normalidad."; }
  else if (PH.failing_since) { dot = "bad"; title = "La sincronización de Fotos está fallando"; detail = last?.message || ""; }
  else if (!last) { title = "Esperando la primera vuelta"; }
  else if (PH.mode === "backfill") {
    dot = "run"; title = "Carga inicial";
    detail = PH.backfill_total
      ? `${nf.format(Math.min(PH.backfill_done_upto, PH.backfill_total))} de ${nf.format(PH.backfill_total)} fotos revisadas, de la más reciente a la más antigua. Cada vuelta baja lo que permiten los límites y sigue en la próxima.`
      : "Empezando por las fotos más recientes. Cada vuelta baja lo que permiten los límites y sigue en la próxima.";
  }
  else if (last.status === "ok") { dot = "ok"; title = last.dry_run ? "Simulación terminada" : "Todo sincronizado"; }
  else if (last.status === "partial") { dot = "warn"; title = "Sincronizado con avisos"; detail = last.message; }
  else if (last.status === "stopped") { dot = "warn"; title = "Parada"; detail = "La próxima vuelta continúa donde se quedó."; }
  else { const [txt, cls] = RUN_STATUS[last.status] || [last.status, "bad"]; dot = cls; title = `Última vuelta: ${txt.toLowerCase()}`; detail = last.message; }
  $("ph-dot").className = "dot " + dot;
  $("ph-title").textContent = title;
  $("ph-detail").textContent = detail || "";

  const sub = [];
  if (PH.running && PH.progress?.elapsed_s != null) sub.push(`en marcha desde hace ${fmtDuration(PH.progress.elapsed_s)}`);
  else if (last?.finished_at) sub.push(`última ${fmtRel(last.finished_at)}`);
  if (!PH.running && auth === "ok") sub.push(`próxima ${fmtRel(PH.next_run_at)}`);
  $("ph-sub").textContent = sub.join(" · ");

  const st = PH.stats || {};
  const chips = [];
  if (PH.forced_dry_run) chips.push(`<span class="chip bad">Simulación forzada (fichero data/SIMULACION_FOTOS)</span>`);
  else if (PH.dry_run) chips.push(`<span class="chip warn">Modo simulación: no cambia nada</span>`);
  if (PH.backoff > 1) chips.push(`<span class="chip warn">Apple está frenando: espera ×${PH.backoff}</span>`);
  chips.push(`<span class="chip">${nf.format(st.fotos || 0)} fotos en el NAS · ${fmtBytes(st.bytes || 0)}</span>`);
  if (PH.last_budget && !PH.running) chips.push(`<span class="chip">Última vuelta: ${esc(PH.last_budget)}</span>`);
  $("ph-chips").innerHTML = chips.join("");

  $("ph-run").classList.toggle("hidden", PH.running);
  $("ph-run").disabled = auth !== "ok";
  $("ph-stop").classList.toggle("hidden", !PH.running);
  $("ph-stop").disabled = !!PH.progress?.stop_requested;
  $("ph-stop").textContent = PH.progress?.stop_requested ? "Parando…" : "Parar";
}

function renderPhotosProgress() {
  const p = PH.progress || {};
  $("ph-progress").classList.toggle("hidden", !PH.running);
  if (!PH.running) return;
  $("ph-progress-title").textContent = p.dry_run ? "Simulando (no se cambia nada)" : "Sincronizando";
  $("ph-elapsed").textContent = p.elapsed_s != null ? fmtDuration(p.elapsed_s) : "";
  const idx = PH_PHASES.findIndex(([k]) => k === p.phase);
  $("ph-steps").innerHTML = PH_PHASES.map(([k, label], i) =>
    `<li class="${i < idx ? "done" : i === idx ? "now" : ""}">${label}</li>`).join("");
  let line = "";
  if (p.phase === "connecting") line = "Empezando…";
  else if (p.phase === "scanning") line = "Comparando con la carpeta del NAS…";
  else if (p.phase === "listing") line = `${esc(p.current_folder || "Leyendo la fototeca…")}`;
  else if (p.phase === "planning") line = "Calculando qué hay que bajar, subir o borrar…";
  else if (p.phase === "transferring" && !p.actions_total) line = "No hay nada que cambiar.";
  $("ph-line").innerHTML = line;

  const transferring = p.phase === "transferring" && p.actions_total > 0;
  $("ph-transfer").classList.toggle("hidden", !transferring);
  if (!transferring) return;
  $("ph-bar").style.width = Math.min(100, 100 * p.actions_done / p.actions_total) + "%";
  $("ph-legend-ops").textContent = `${nf.format(p.actions_done)} de ${nf.format(p.actions_total)} (sin pasar del límite de la vuelta)`;
  $("ph-legend-bytes").textContent = `${fmtBytes(p.bytes_done)}${p.speed_bps ? " · " + fmtBytes(p.speed_bps) + "/s" : ""}`;
  const c = p.current;
  $("ph-current").innerHTML = c ? `${esc(_t(PH_ACTIONS[c.kind] || c.kind))} ${pathHtml(c.path)}` +
    (c.size ? ` <span class="muted">(${fmtBytes(c.size)})</span>` : "") : "";
  $("ph-counters").innerHTML = p.errors ? `<span class="bad">✕ ${nf.format(p.errors)} con error</span>` : "";
}

function renderPhotosRuns() {
  $("ph-runs").innerHTML = (PH.runs || []).map(r => {
    const [txt, cls] = r.finished_at ? (RUN_STATUS[r.status] || [r.status, ""]) : ["En curso", ""];
    const errs = (r.errors || []).filter(e => !e.startsWith("aviso:"));
    const warns = (r.errors || []).filter(e => e.startsWith("aviso:"));
    const list = (arr, label) => arr.length ? `<details><summary>${arr.length} ${label}</summary><ul class="list">${arr.slice(0, 50).map(e => `<li>${esc(e.replace(/^aviso: /, ""))}</li>`).join("")}</ul></details>` : "";
    const bytes = [r.bytes_down ? _t("{b} bajados", { b: fmtBytes(r.bytes_down) }) : "", r.bytes_up ? _t("{b} subidos", { b: fmtBytes(r.bytes_up) }) : ""].filter(Boolean).join(", ");
    const summary = phCounts(r.counts) || r.message;
    return `<li><span class="when" title="${esc(fmtAbs(r.started_at))}">${esc(fmtRel(r.started_at))}
        ${r.duration_s != null ? `<span class="muted small">· ${fmtDuration(r.duration_s)}</span>` : ""}</span>
      <span class="st ${cls}">${esc(txt)}${r.dry_run ? " · simulación" : ""}</span>
      <span class="summary">${esc(summary)} ${bytes ? `<span class="muted small">${esc(bytes)}</span>` : ""}
        ${r.message && summary !== r.message ? `<div class="muted small">${esc(r.message)}</div>` : ""}</span>
      ${list(errs, "errores")}${list(warns, "avisos")}</li>`;
  }).join("") || `<li class="muted">Todavía no hay ninguna vuelta de Fotos.</li>`;
}

function renderPhotosConfig() {
  const c = PH.config || {};
  $("ph-dir").textContent = c.dir || "";
  $("ph-upload").textContent = `${c.upload_dir || "Subir"}/`;
  $("ph-albums-dir").textContent = `${c.albums_dir || "Álbumes"}/`;      // los nombres en disco dependen de NAMES_LANG
  $("ph-edited-name").textContent = c.edited_name || "(editada)";
  $("ph-confirm").textContent = nf.format(c.confirm_deletes_over ?? 5);
  $("ph-limits").textContent = `${nf.format(c.max_downloads || 0)} descargas o ${c.max_gb} GB, ` +
    `${nf.format(c.max_uploads || 0)} subidas y ${nf.format(c.max_deletes || 0)} borrados`;
  const rows = [
    ["Carpeta del NAS", c.dir], ["Para subir a Fotos", `${c.dir}/${c.upload_dir}`],
    ["Álbumes (enlaces, sin ocupar más)", `${c.dir}/${c.albums_dir || "Álbumes"}`],
    ["Ocultas", c.hidden ? c.hidden_dir : "No se bajan (PHOTOS_HIDDEN=false)"],
    ["Cada", `${c.interval_min} min (más si Apple frena)`],
    ["Cómo revisa iCloud", PH.mode === "backfill" ? "Carga inicial por bloques, de lo más reciente a lo más antiguo"
      : PH.mode === "changes" ? "Solo la lista de cambios de Apple (y una vuelta completa a la semana)" : "Vuelta completa (red de seguridad semanal)"],
    ["Modo", PH.dry_run ? "Simulación (PHOTOS_DRY_RUN=true): no cambia nada" : "Real"],
    ["Por vuelta, como mucho", `${nf.format(c.max_downloads)} descargas, ${c.max_gb} GB, ${nf.format(c.max_uploads)} subidas, ${nf.format(c.max_deletes)} borrados`],
    ["Pedir confirmación si se borran más de", `${c.confirm_deletes_over} fotos`],
  ];
  $("ph-config").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

async function loadPhotosPending() {
  const n = PH?.pending_deletes || 0;
  $("ph-pending").classList.toggle("hidden", !n);
  if (!n) { phPendingBatch = null; phPendingFor = null; return; }
  if (phPendingFor === PH.pending_batch && phPendingBatch) return;
  const s = await api("/api/photos/pending-deletes");
  if (!s.batch_id) return;
  phPendingBatch = s.batch_id; phPendingFor = PH.pending_batch; phPendingGroups = s.folders;
  $("ph-pending-text").textContent = s.text;
  $("ph-pending-folders").innerHTML = s.folders.map((f, idx) => `<details class="pfolder">
      <summary>
        <span class="name"><b>${esc(f.folder || "/")}</b><br><span class="side">${esc(f.side_text)}</span></span>
        <span class="small">${nf.format(f.count)} foto(s)${f.bytes ? ` · ${fmtBytes(f.bytes)}` : ""}</span>
        <span class="btns">
          <button class="danger" data-g="${idx}" data-d="confirm">Borrar</button>
          <button data-g="${idx}" data-d="reject">Restaurar</button>
        </span>
      </summary>
      <ul class="list small">${f.items.map(i => `<li><span class="icon">🗑</span><div class="what">
        <div class="path">${pathHtml(i.path)}</div>${i.size ? `<div class="meta">${fmtBytes(i.size)}</div>` : ""}</div></li>`).join("")}</ul>
    </details>`).join("");
}

async function refreshPhotos() {
  try {
    PH = await api("/api/photos/status");
  } catch (e) {
    if (e.http === 404) { PH = null; photosDisabled(); }   // SYNC_PHOTOS=false
    return;
  }
  if (phWasRunning && !PH.running) phPendingFor = null;
  phWasRunning = PH.running;
  renderPhotosStatus(); renderPhotosProgress(); renderPhotosRuns(); renderPhotosConfig();
  renderTabDots();
  await loadPhotosPending().catch(() => {});
}

function photosDisabled() {
  $("ph-dot").className = "dot";
  $("ph-title").textContent = "Desactivada";
  $("ph-detail").textContent = "La sincronización de Fotos está apagada (SYNC_PHOTOS=false en el stack).";
  for (const id of ["ph-run", "ph-stop"]) $(id).classList.add("hidden");
}

async function photosRun(dry) {
  try { const r = await post(`/api/photos/run${dry ? "?dry_run=true" : ""}`); toast(r.message, !r.ok); }
  catch (e) { toast(e.message, true); }
  setTimeout(kickPhotos, 500);
}

async function photosDecideAll(decision) {
  if (!phPendingBatch) return;
  const msg = decision === "confirm"
    ? "¿Borrar TODO el lote? Las fotos borradas en el NAS se borrarán en iCloud (a Eliminados recientemente, 30 días) y las borradas en iCloud irán a la papelera del NAS."
    : "¿Restaurar TODO el lote? Las borradas en el NAS se vuelven a bajar de iCloud; las borradas en iCloud se quedan en el NAS.";
  if (!i18n.confirm(msg)) return;
  try { const r = await post(`/api/photos/pending-deletes/${phPendingBatch}/${decision}`); toast(r.message, !r.ok); }
  catch (e) { toast(e.message, true); }
  phPendingFor = null; kickPhotos();
}

$("ph-run").onclick = () => photosRun(false);
$("ph-stop").onclick = async () => {
  if (!i18n.confirm("¿Parar Fotos al terminar el fichero actual? La próxima vuelta continuará donde se quede.")) return;
  try { const r = await post("/api/photos/stop"); toast(r.message, !r.ok); } catch (e) { toast(e.message, true); }
  kickPhotos();
};
$("ph-pending-folders").onclick = async e => {
  const b = e.target.closest("button[data-g]");
  if (!b) return;
  e.preventDefault();
  const g = phPendingGroups[Number(b.dataset.g)];
  if (!g || !phPendingBatch) return;
  const where = g.side === "icloud" ? "también en iCloud (a Eliminados recientemente, 30 días)" : "del NAS (a su papelera)";
  const msg = b.dataset.d === "confirm" ? `¿Borrar ${g.count} foto(s) de «${g.folder || "/"}» ${where}?`
    : `¿Restaurar ${g.count} foto(s) de «${g.folder || "/"}»?`;
  if (!i18n.confirm(msg)) return;
  try {
    const r = await post(`/api/photos/pending-deletes/${phPendingBatch}/decide`, { folder: g.folder, side: g.side, decision: b.dataset.d });
    toast(r.message, !r.ok);
  } catch (err) { toast(err.message, true); }
  phPendingFor = null; kickPhotos();
};
$("ph-pending-confirm").onclick = () => photosDecideAll("confirm");
$("ph-pending-reject").onclick = () => photosDecideAll("reject");

// Más rápido con la pestaña Fotos a la vista o con una vuelta en marcha
const kickPhotos = (() => {
  let timer = null, busy = false;
  const tick = async () => {
    clearTimeout(timer);
    if (busy) return;
    busy = true;
    try { await refreshPhotos(); } finally { busy = false; }
    const visible = currentTab === "fotos" && !document.hidden;
    timer = setTimeout(tick, document.hidden ? 60000 : PH?.running && visible ? 2000 : visible ? 10000 : 30000);
  };
  tick();
  return tick;
})();
