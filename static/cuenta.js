// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = t => new Date(t * 1000).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
const KIND = { password: "contraseña", totp: "código", csrf: "petición de otro origen", sin_sesion: "acceso sin sesión",
               reset_request: "pidió código por email", reset_code: "código del email" };

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

async function call(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.replace("/login?next=/cuenta"); throw new Error("Sesión caducada"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || data.detail || `Error ${r.status}`);
  return data;
}

function device(ua) {
  ua = ua || "";
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
    : /Mac OS/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "Dispositivo";
  const br = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "";
  return br ? `${br} en ${os}` : os;
}

async function load() {
  const a = await call("GET", "/auth/account");
  $("who").textContent = `Cuenta de ${a.user}`;
  $("your-ip").textContent = a.your_ip;
  $("rec-left").textContent = a.recovery_left;
  $("sessions").innerHTML = a.sessions.map(s => `<li><div class="what"><b>${esc(device(s.ua))}</b>${s.current ? ' <span class="chip">esta</span>' : ""}
      <div class="meta">${esc(s.ip)} · último uso ${esc(when(s.last_seen))} · caduca ${esc(when(s.expires))}</div></div>
      ${s.current ? "" : `<button data-revoke="${esc(s.id)}">Cerrar</button>`}</li>`).join("") || `<li class="muted">Ninguna</li>`;
  $("bans").innerHTML = a.bans.map(b => `<li><div class="what"><b>${esc(b.ip)}</b>
      <div class="meta">${esc(b.reason)} · hasta ${esc(when(b.until))}${b.manual ? " · manual" : ` · nivel ${b.level + 1}`}</div></div>
      <button data-unban="${esc(b.ip)}">Desbloquear</button></li>`).join("") || `<li class="muted">Ninguna IP bloqueada</li>`;
  $("attempts").innerHTML = a.attempts.map(t => `<li><span class="${t.ok ? "ok" : "bad"}">${t.ok ? "✓" : "✕"}</span>
      <div class="what">${esc(when(t.ts))} · ${esc(t.ip)}${t.user ? ` · ${esc(t.user)}` : ""} · ${esc(KIND[t.kind] || t.kind)}</div></li>`).join("");
}

$("sessions").onclick = async e => {
  const id = e.target.dataset?.revoke;
  if (!id) return;
  try { toast((await call("POST", "/auth/sessions/revoke", { id })).message); } catch (err) { toast(err.message, true); }
  load();
};
$("revoke-all").onclick = async () => {
  if (!confirm("¿Cerrar todas las sesiones salvo esta?")) return;
  try { toast((await call("POST", "/auth/sessions/revoke", { all: true })).message); } catch (err) { toast(err.message, true); }
  load();
};
$("bans").onclick = async e => {
  const ip = e.target.dataset?.unban;
  if (!ip) return;
  try { toast((await call("POST", "/auth/bans", { ip, action: "unban" })).message); } catch (err) { toast(err.message, true); }
  load();
};
$("form-ban").onsubmit = async e => {
  e.preventDefault();
  const ip = $("ban-ip").value.trim();
  if (!ip || !confirm(`¿Bloquear ${ip} durante un año?`)) return;
  try { toast((await call("POST", "/auth/bans", { ip, action: "ban" })).message); $("ban-ip").value = ""; } catch (err) { toast(err.message, true); }
  load();
};
$("form-password").onsubmit = async e => {
  e.preventDefault();
  try {
    toast((await call("POST", "/auth/password", { current: $("pw-current").value, new: $("pw-new").value, code: $("pw-code").value })).message);
    e.target.reset();
  } catch (err) { toast(err.message, true); }
  load();
};
$("form-recovery").onsubmit = async e => {
  e.preventDefault();
  if (!confirm("Los códigos anteriores dejarán de valer. ¿Generar nuevos?")) return;
  try {
    const r = await call("POST", "/auth/recovery", { code: $("rec-code").value });
    $("rec-codes").textContent = r.recovery_codes.join("\n");
    $("rec-codes").classList.remove("hidden");
    toast("Códigos nuevos: guárdalos ahora, no se volverán a mostrar");
    e.target.reset();
  } catch (err) { toast(err.message, true); }
  load();
};
$("logout").onclick = async () => { await call("POST", "/auth/logout").catch(() => {}); location.replace("/login"); };

// Botón "ver contraseña" en cada campo de contraseña
function addPasswordToggles() {
  for (const input of document.querySelectorAll('input[type="password"]')) {
    if (input.dataset.toggle) continue;
    input.dataset.toggle = "1";
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.textContent = "👁";
    btn.setAttribute("aria-label", "Mostrar contraseña");
    btn.setAttribute("aria-pressed", "false");
    btn.onclick = () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", String(show));
      btn.setAttribute("aria-label", show ? "Ocultar contraseña" : "Mostrar contraseña");
      btn.classList.toggle("on", show);
      input.focus();
    };
    wrap.appendChild(btn);
  }
}
addPasswordToggles();

$("form-reset-totp").onsubmit = async e => {
  e.preventDefault();
  if (!confirm("Se borrará el segundo factor actual y tus códigos de recuperación, se cerrarán las demás sesiones y tendrás que escanear un QR nuevo ahora mismo. ¿Seguir?")) return;
  try {
    const r = await call("POST", "/auth/totp/reset", { password: $("rt-password").value, code: $("rt-code").value });
    location.replace(r.next || "/login");
  } catch (err) { toast(err.message, true); }
};

load().catch(err => toast(err.message, true));
