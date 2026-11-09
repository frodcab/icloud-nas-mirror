// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
//
// Idioma del panel: español (el de los textos del código) o inglés. Elige el idioma por este orden: el que guardó el
// usuario (localStorage o cookie `lang`), el del navegador, español. En español no hace nada más. En otro idioma carga
// /static/i18n-<idioma>.json (síncrono, para no ver un instante el texto en español), traduce la página y sigue
// traduciendo lo que el resto de scripts vaya pintando. Se expone `_t(texto, {parámetros})` para textos compuestos.
(function () {
  "use strict";
  const SUPPORTED = ["es", "en"];
  const SOURCE = "es";
  const ATTRS = ["placeholder", "title", "aria-label", "alt"];

  function stored() {
    let l = null;
    try { l = localStorage.getItem("lang"); } catch (e) { /* modo privado */ }
    if (!l) { const m = document.cookie.match(/(?:^|; )lang=(\w+)/); l = m && m[1]; }
    return SUPPORTED.includes(l) ? l : null;
  }
  function pick() {
    const saved = stored();
    if (saved) return saved;
    for (const c of (navigator.languages || [navigator.language || ""])) {
      const base = String(c).toLowerCase().slice(0, 2);
      if (SUPPORTED.includes(base)) return base;
    }
    return SOURCE;
  }

  const lang = pick();
  document.documentElement.lang = lang;

  let catalog = {};
  if (lang !== SOURCE) {
    try {
      const x = new XMLHttpRequest();
      x.open("GET", "/static/i18n-" + lang + ".json", false);
      x.send();
      if (x.status === 200) catalog = JSON.parse(x.responseText);
    } catch (e) { /* sin catálogo: se queda en español */ }
  }
  const core = I18nCore.makeTranslator(catalog);

  function set(l) {
    if (!SUPPORTED.includes(l)) return;
    try { localStorage.setItem("lang", l); } catch (e) { /* ignore */ }
    document.cookie = "lang=" + l + "; path=/; max-age=31536000; SameSite=Lax";
    location.reload();
  }

  window._t = (key, params) => core.t(key, params);
  const locale = lang === "en" ? "en-GB" : "es-ES";      // para fechas, números y tamaños
  // tr: traduce un texto ya compuesto (o lo devuelve igual); confirm: lo mismo para los diálogos, que no pasan por el DOM
  const tr = text => { const out = core.tr(text); return out != null ? out : text; };
  window.i18n = { lang, locale, supported: SUPPORTED, set, tr, confirm: text => window.confirm(tr(text)) };

  function renderSwitch() {
    document.querySelectorAll("#lang-switch").forEach(el => {
      el.textContent = "";
      SUPPORTED.forEach((l, i) => {
        if (i) el.append(" · ");
        const item = document.createElement(l === lang ? "b" : "button");
        item.textContent = l.toUpperCase();
        if (l !== lang) { item.className = "link"; item.type = "button"; item.title = l === "es" ? "Español" : "English"; item.onclick = () => set(l); }
        el.append(item);
      });
    });
  }

  if (lang !== SOURCE) {
    const translateText = node => {
      const value = node.nodeValue, key = I18nCore.norm(value);
      if (!key) return;
      const out = core.tr(key);
      if (out != null && out !== key) node.nodeValue = value.match(/^\s*/)[0] + out + value.match(/\s*$/)[0];
    };
    const translateAttrs = el => {
      for (const a of ATTRS) {
        if (!el.hasAttribute(a)) continue;
        const v = el.getAttribute(a), out = core.tr(v);
        if (out != null && out !== v) el.setAttribute(a, out);
      }
    };
    const walk = node => {
      if (node.nodeType === 3) { translateText(node); return; }
      if (node.nodeType !== 1 || node.tagName === "SCRIPT" || node.tagName === "STYLE") return;
      if (node.hasAttribute("data-i18n-html")) {        // párrafo con etiquetas dentro: se traduce entero
        const out = core.tr(node.innerHTML);
        if (out != null) node.innerHTML = out;
        return;
      }
      translateAttrs(node);
      for (let c = node.firstChild; c; c = c.nextSibling) walk(c);
    };
    const options = { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS };
    const observer = new MutationObserver(records => {
      observer.disconnect();                              // lo que escribimos nosotros no vuelve a entrar
      for (const r of records) {
        if (r.type === "childList") r.addedNodes.forEach(walk);
        else if (r.type === "characterData") translateText(r.target);
        else if (r.type === "attributes") translateAttrs(r.target);
      }
      observer.observe(document.documentElement, options);
    });
    const start = () => { walk(document.documentElement); renderSwitch(); observer.observe(document.documentElement, options); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderSwitch);
  } else {
    renderSwitch();
  }
})();
