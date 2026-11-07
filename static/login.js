// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
"use strict";
const $ = id => document.getElementById(id);
const next = new URLSearchParams(location.search).get("next") || "/";
let afterCodes = "/";

function show(step) {
  for (const s of ["step-password", "step-2fa", "step-setup", "step-codes", "step-forgot", "step-forgot-code"]) $(s).classList.toggle("hidden", s !== step);
  const first = document.querySelector(`#${step} input`);
  if (first) first.focus();
}

function say(text, ok = false) { $("msg").className = "form-msg " + (ok ? "ok" : "bad"); $("msg").textContent = text || ""; }

async function call(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await r.json(); } catch { data = { message: await r.text().catch(() => "") }; }
  if (!r.ok) throw new Error(data.message || data.detail || `Error ${r.status}`);
  return data;
}

function busy(form, on) { for (const b of form.querySelectorAll("button")) b.disabled = on; }

async function startSetup() {
  const s = await call("GET", "/auth/setup");
  $("qr").innerHTML = s.qr_svg;              // SVG generado por el servidor (segno)
  $("secret").textContent = s.secret.replace(/(.{4})/g, "$1 ").trim();
  $("otp-link").href = s.uri;              // otpauth:// → abre la app de autenticación del móvil
  $("copy-secret").onclick = () => navigator.clipboard?.writeText(s.secret).then(() => say("Clave copiada", true));
  show("step-setup");
}

$("form-password").onsubmit = async e => {
  e.preventDefault(); say(""); busy(e.target, true);
  try {
    const r = await call("POST", "/auth/login", { user: $("user").value, password: $("password").value, remember: $("remember").checked });
    $("password").value = "";
    if (r.next === "setup") await startSetup(); else show("step-2fa");
  } catch (err) { say(err.message); } finally { busy(e.target, false); }
};

$("form-2fa").onsubmit = async e => {
  e.preventDefault(); say(""); busy(e.target, true);
  try {
    const r = await call("POST", "/auth/2fa", { code: $("code").value, next });
    location.replace(r.next || "/");
  } catch (err) { say(err.message); $("code").value = ""; $("code").focus(); } finally { busy(e.target, false); }
};
$("code").oninput = e => { if (/^\d{6}$/.test(e.target.value)) $("form-2fa").requestSubmit(); };

$("form-setup").onsubmit = async e => {
  e.preventDefault(); say(""); busy(e.target, true);
  try {
    const r = await call("POST", "/auth/setup", { code: $("setup-code").value, next });
    afterCodes = r.next || "/";
    $("codes").textContent = r.recovery_codes.join("\n");
    show("step-codes");
  } catch (err) { say(err.message); } finally { busy(e.target, false); }
};

// --- ¿has olvidado la contraseña? ---
$("go-forgot").onclick = () => { say(""); $("forgot-user").value = $("user").value; show("step-forgot"); };
for (const b of document.querySelectorAll(".back-login")) b.onclick = () => { say(""); show("step-password"); };
$("form-forgot").onsubmit = async e => {
  e.preventDefault(); say(""); busy(e.target, true);
  try {
    const r = await call("POST", "/auth/forgot", { user: $("forgot-user").value });
    $("forgot-sent").textContent = r.message;
    show("step-forgot-code");
  } catch (err) { say(err.message); } finally { busy(e.target, false); }
};
$("form-forgot-code").onsubmit = async e => {
  e.preventDefault(); say(""); busy(e.target, true);
  try {
    const r = await call("POST", "/auth/forgot/confirm", { user: $("forgot-user").value, code: $("forgot-code").value, new: $("forgot-new").value });
    e.target.reset();
    $("user").value = $("forgot-user").value;
    show("step-password");
    say(r.message, true);
  } catch (err) { say(err.message); } finally { busy(e.target, false); }
};

$("copy-codes").onclick = () => navigator.clipboard?.writeText($("codes").textContent).then(() => say("Copiados", true));
$("codes-done").onclick = () => location.replace(afterCodes);

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

// Si ya hay una sesión a medias (contraseña hecha), se sigue donde se quedó
call("GET", "/auth/state").then(s => {
  document.title = `Entrar · ${s.app}`;
  if (s.user && s.mfa) location.replace(next);
  else if (s.user && s.needs_totp_setup) startSetup().catch(err => say(err.message));
  else if (s.user) show("step-2fa");
}).catch(() => {});
