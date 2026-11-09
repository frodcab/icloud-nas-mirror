// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
//
// Comprobación OPCIONAL del cargador de idioma (static/i18n.js) con un DOM simulado: traduce las páginas, observa lo que
// pinta el resto de scripts, elige el idioma y pone el selector. Necesita jsdom, que NO es dependencia de la app:
//   mkdir /tmp/jsdom && cd /tmp/jsdom && npm install jsdom
//   JSDOM_DIR=/tmp/jsdom node tests/js/i18n_dom_check.js        (o con pytest: JSDOM_DIR=/tmp/jsdom pytest tests/test_i18n.py)
const path = require("path");
const { JSDOM } = require(require.resolve("jsdom", { paths: [process.env.JSDOM_DIR || process.cwd()] }));
const fs = require("fs");
const STATIC = path.join(__dirname, "..", "..", "static") + path.sep;
const results = [];
const ok = (name, cond, extra = "") => { results.push(cond); console.log((cond ? "✓ " : "✗ ") + name + (cond ? "" : "  " + extra)); };

function load(page, lang) {
  const html = fs.readFileSync(STATIC + page, "utf8");
  const dom = new JSDOM(html, { url: "http://localhost:8096/" + page, runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  Object.defineProperty(w.navigator, "languages", { value: lang ? [lang] : [], configurable: true });
  Object.defineProperty(w.navigator, "language", { value: lang || "", configurable: true });
  // XMLHttpRequest síncrono simulado: sirve el catálogo del disco
  w.XMLHttpRequest = function () {
    this.open = (m, url) => { this.url = url; };
    this.send = () => { const f = STATIC + this.url.replace("/static/", ""); if (fs.existsSync(f)) { this.status = 200; this.responseText = fs.readFileSync(f, "utf8"); } else this.status = 404; };
  };
  w.eval(fs.readFileSync(STATIC + "i18n-core.js", "utf8"));
  w.eval(fs.readFileSync(STATIC + "i18n.js", "utf8"));
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  return w;
}
const text = (w, sel) => w.document.querySelector(sel).textContent.replace(/\s+/g, " ").trim();

(async () => {
  // 1) Navegador en inglés
  let w = load("login.html", "en-GB");
  ok("lang del <html> = en", w.document.documentElement.lang === "en");
  ok("título traducido", w.document.title === "Sign in · iCloud sync", w.document.title);
  ok("texto: 'Inicia sesión para continuar.'", text(w, "#step-password p.muted") === "Sign in to continue.", text(w, "#step-password p.muted"));
  ok("botón Entrar → Sign in", text(w, "#form-password button.primary") === "Sign in", text(w, "#form-password button.primary"));
  ok("etiqueta con input dentro: 'Usuario'", /^Username/.test(text(w, "#form-password label")), text(w, "#form-password label"));
  ok("placeholder/aria traducido", [...w.document.querySelectorAll("[aria-label],[placeholder]")].some(e => e.getAttribute("aria-label") === "App code"));
  ok("párrafo con <b> traducido entero", w.document.querySelector("#step-codes p").innerHTML.includes("<b>only once</b>"), w.document.querySelector("#step-codes p").innerHTML);
  ok("selector de idioma: EN en negrita, ES botón", w.document.querySelector("#lang-switch b").textContent === "EN" && w.document.querySelector("#lang-switch button").textContent === "ES");
  ok("_t() compone con catálogo", w._t("Cargando…") === "Loading…");

  // 2) Texto que pinta el JS después (observador)
  const p = w.document.createElement("p"); p.id = "nuevo"; p.textContent = "Cargando dispositivos…"; w.document.body.appendChild(p);
  await new Promise(r => setTimeout(r, 30));
  ok("observador traduce lo añadido después", p.textContent === "Loading devices…", p.textContent);
  p.textContent = "Todo"; await new Promise(r => setTimeout(r, 30));
  ok("observador traduce un cambio de texto", p.textContent === "All", p.textContent);
  p.textContent = "Mensaje que no existe"; await new Promise(r => setTimeout(r, 30));
  ok("lo desconocido se queda como está (sin bucles)", p.textContent === "Mensaje que no existe");

  // 3) Elección del idioma
  const w2 = load("login.html", "es-ES");
  ok("navegador en español: no se toca nada", w2.document.title === "Entrar · iCloud sync" && w2.document.documentElement.lang === "es");
  ok("español: _t devuelve el original", w2._t("Cargando…") === "Cargando…");
  const w3 = load("login.html", "fr-FR");
  ok("idioma no soportado: español", w3.document.documentElement.lang === "es");
  const w4 = (() => { const d = new JSDOM(fs.readFileSync(STATIC + "login.html", "utf8"), { url: "http://localhost:8096/login", runScripts: "outside-only" }); const x = d.window;
    x.localStorage.setItem("lang", "en"); Object.defineProperty(x.navigator, "languages", { value: ["es-ES"] });
    x.XMLHttpRequest = function () { this.open = (m, u) => { this.u = u; }; this.send = () => { this.status = 200; this.responseText = fs.readFileSync(STATIC + this.u.replace("/static/", ""), "utf8"); }; };
    x.eval(fs.readFileSync(STATIC + "i18n-core.js", "utf8")); x.eval(fs.readFileSync(STATIC + "i18n.js", "utf8")); x.document.dispatchEvent(new x.Event("DOMContentLoaded")); return x; })();
  ok("lo guardado manda sobre el navegador", w4.document.documentElement.lang === "en");

  // 4) Las otras páginas
  const wi = load("index.html", "en");
  ok("index traducido (pestañas)", text(wi, 'button[data-tab="archivos"]').startsWith("📁 Files"), text(wi, 'button[data-tab="archivos"]'));
  ok("index: frase partida alrededor de <b>", wi.document.body.textContent.includes("Each photo is saved in") && wi.document.body.textContent.includes("YYYY/MM"));
  const wc = load("cuenta.html", "en");
  ok("cuenta traducida", text(wc, "h1, h2") !== "" && wc.document.title === "Account · iCloud sync", wc.document.title);
  process.exit(results.every(Boolean) ? 0 : 1);
})();
