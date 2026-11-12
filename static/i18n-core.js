// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 F. Javier R. Cabero
//
// Núcleo de traducción, sin DOM (se prueba con Node). El texto en ESPAÑOL es la clave: el catálogo de otro idioma
// (i18n-en.json) mapea "texto en español" -> "texto traducido". Las claves con {nombre} son plantillas: sirven tanto
// para componer (`t("Hola {n}", {n: 3})`) como para reconocer un texto ya compuesto ("Hola 3") y traducirlo.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.I18nCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const norm = s => String(s).replace(/\s+/g, " ").trim();
  const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Frases de un texto: se corta tras . ! ? seguidos de espacio
  function sentences(k) {
    const out = [];
    let start = 0, m;
    const re = /[.!?](?=\s)/g;
    while ((m = re.exec(k))) { out.push(k.slice(start, m.index + 1).trim()); start = m.index + 1; }
    out.push(k.slice(start).trim());
    return out.filter(Boolean);
  }
  const fill = (text, values) => text.replace(/\{(\w+)\}/g, (m, name) => (name in values ? String(values[name]) : m));

  function compile(catalog) {
    const exact = new Map();
    const byKey = new Map();            // todas las entradas por su clave (también las plantillas), para t()
    const templates = [];
    for (const [key, out] of Object.entries(catalog)) {
      const k = norm(key);
      byKey.set(k, out);
      if (/\{\w+\}/.test(k)) {
        // Una plantilla que EMPIEZA por un hueco y casi no tiene texto fijo ("{a} de {b}") casaría con cualquier frase:
        // solo sirve para componer con t(), no para reconocer texto suelto. {n} casa solo números, así que "{n} fotos" sí vale.
        const literal = k.replace(/\{\w+\}/g, "").replace(/\s/g, "").length;
        if (k.startsWith("{") && !k.startsWith("{n}") && literal < 10) continue;
        const names = [];
        const source = k.split(/(\{\w+\})/).map(part => {
          const m = part.match(/^\{(\w+)\}$/);
          if (m) { names.push(m[1]); return m[1] === "n" ? "(\\d[\\d.,]*)" : "((?:(?! · )[\\s\\S])+?)"; }
          return escapeRe(part);
        }).join("");
        templates.push({ re: new RegExp("^" + source + "$"), names, out, weight: k.replace(/\{\w+\}/g, "").length });
      } else {
        exact.set(k, out);
      }
    }
    templates.sort((a, b) => b.weight - a.weight);      // las plantillas más específicas primero
    return { exact, byKey, templates };
  }

  function makeTranslator(catalog) {
    const { exact, byKey, templates } = compile(catalog || {});

    // Para reconocer texto en minúsculas ("todo sincronizado") que en el catálogo está con mayúscula inicial
    const lower = new Map();
    for (const [k, out] of exact) lower.set(k.toLowerCase(), [k, out]);

    // Texto ya compuesto -> traducido (o null si el catálogo no lo conoce). Los valores que lleva dentro una plantilla
    // ("Última sincronización: {x}") se traducen a su vez si el catálogo los conoce.
    function tr(text, depth) {
      const k = norm(text);
      if (!k) return null;
      if (exact.has(k)) return exact.get(k);
      const hit = lower.get(k.toLowerCase());
      if (hit && k[0] !== k[0].toUpperCase()) {            // vino en minúscula: la traducción también
        const out = hit[1];
        return out.charAt(0).toLowerCase() + out.slice(1);
      }
      for (const t of templates) {
        const m = k.match(t.re);
        if (m) {
          const values = {};
          t.names.forEach((n, i) => {
            const raw = m[i + 1];
            const inner = (depth || 0) < 3 ? tr(raw, (depth || 0) + 1) : null;
            // se conservan los espacios de los extremos (" y 2 carpetas" -> " and 2 folders")
            values[n] = inner != null ? raw.match(/^\s*/)[0] + inner + raw.match(/\s*$/)[0] : raw;
          });
          return fill(t.out, values);
        }
      }
      if ((depth || 0) < 3) {
        // Una línea compuesta con " · " ("Descargado de iCloud · 12 KB · 10:31"): cada parte por separado
        if (k.indexOf(" · ") >= 0) {
          const segs = k.split(" · ");
          const outs = segs.map(p => tr(p, (depth || 0) + 1));
          if (outs.some(o => o != null)) return segs.map((p, i) => (outs[i] != null ? outs[i] : p)).join(" · ");
        }
        // Varias frases ("Se aplica ahora." + otra): cada una por separado; si alguna se conoce, se unen
        const parts = sentences(k);
        if (parts.length > 1) {
          const outs = parts.map(p => tr(p, (depth || 0) + 1));
          if (outs.some(o => o != null)) return parts.map((p, i) => (outs[i] != null ? outs[i] : p)).join(" ");
        }
        // Una frase con punto final: las claves no lo llevan
        if (/[.!?]$/.test(k) && !k.endsWith("...")) {
          const r = tr(k.slice(0, -1), (depth || 0) + 1);
          if (r != null) return r + k.slice(-1);
        }
      }
      return null;
    }

    // Para componer en el código: t("{n} fotos", {n: 3}). Sin traducción conocida, se usa el propio texto.
    function t(key, params) {
      const found = byKey.get(norm(key));
      const text = found !== undefined ? found : key;
      return params ? fill(text, params) : text;
    }

    return { tr, t, size: exact.size + templates.length };
  }

  return { norm, makeTranslator };
});
