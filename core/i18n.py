# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Traducción en el servidor (correos y avisos). Es el gemelo de static/i18n-core.js y usa el MISMO catálogo.

El texto en español es la clave; `static/i18n-<idioma>.json` lo traduce. Las claves con {nombre} son plantillas: reconocen
un texto ya compuesto ("Sesiones cerradas: 3") y su traducción lleva los mismos valores. Un texto de varias frases se
traduce frase a frase; lo que el catálogo no conoce se deja como está (nunca se pierde información).
"""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "static"
SOURCE_LANG = "es"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _fill(text: str, values: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def _sentences(k: str) -> list[str]:
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s)", k):
        out.append(k[start:m.end()].strip())
        start = m.end()
    out.append(k[start:].strip())
    return [s for s in out if s]


class Translator:
    def __init__(self, catalog: dict):
        self.exact, self.by_key, self.templates = {}, {}, []
        for key, out in (catalog or {}).items():
            k = norm(key)
            self.by_key[k] = out
            if re.search(r"\{\w+\}", k):
                # Plantilla que EMPIEZA por un hueco y casi no tiene texto fijo ("{a} de {b}"): casaría con cualquier frase;
                # solo sirve para componer con t(). {n} casa solo números, así que "{n} fotos" sí vale.
                literal = len(re.sub(r"\s", "", re.sub(r"\{\w+\}", "", k)))
                if k.startswith("{") and not k.startswith("{n}") and literal < 10:
                    continue
                names, source = [], []
                for part in re.split(r"(\{\w+\})", k):
                    m = re.fullmatch(r"\{(\w+)\}", part)
                    if m:
                        names.append(m.group(1))
                        source.append(r"(\d[\d.,]*)" if m.group(1) == "n" else r"([\s\S]+?)")
                    else:
                        source.append(re.escape(part))
                weight = len(re.sub(r"\{\w+\}", "", k))
                self.templates.append((weight, re.compile("^" + "".join(source) + "$"), names, out))
            else:
                self.exact[k] = out
        self.templates.sort(key=lambda t: -t[0])
        self.lower = {k.lower(): out for k, out in self.exact.items()}

    def tr(self, text: str, depth: int = 0) -> str | None:
        """Texto ya compuesto -> traducido, o None si el catálogo no lo conoce."""
        k = norm(text)
        if not k:
            return None
        if k in self.exact:
            return self.exact[k]
        low = self.lower.get(k.lower())
        if low is not None and k[0] != k[0].upper():           # vino en minúscula: la traducción también
            return low[:1].lower() + low[1:]
        for _w, rx, names, out in self.templates:
            m = rx.match(k)
            if m:
                values = {}
                for i, n in enumerate(names):
                    raw = m.group(i + 1)
                    inner = self.tr(raw, depth + 1) if depth < 3 else None
                    if inner is not None:
                        lead, trail = raw[:len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
                        values[n] = lead + inner + trail
                    else:
                        values[n] = raw
                return _fill(out, values)
        if depth < 3:
            parts = _sentences(k)
            if len(parts) > 1:
                outs = [self.tr(p, depth + 1) for p in parts]
                if any(o is not None for o in outs):
                    return " ".join(o if o is not None else p for p, o in zip(parts, outs))
            if re.search(r"[.!?]$", k) and not k.endswith("..."):
                r = self.tr(k[:-1], depth + 1)
                if r is not None:
                    return r + k[-1]
        return None

    def t(self, key: str, **params) -> str:
        text = self.by_key.get(norm(key), key)
        return _fill(text, params) if params else text


@lru_cache(maxsize=8)
def translator(lang: str) -> Translator | None:
    """Traductor del idioma, o None si es el de origen o no hay catálogo."""
    if not lang or lang == SOURCE_LANG:
        return None
    try:
        return Translator(json.loads((STATIC / f"i18n-{lang}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        log.warning("No catalog for language %r (%s): texts stay in Spanish", lang, exc)
        return None


def translate_line(text: str, lang: str) -> str:
    tr = translator(lang)
    if tr is None:
        return text
    out = tr.tr(text)
    return out if out is not None else text


def translate_text(text: str, lang: str) -> str:
    """Un texto de varias líneas (cuerpo de un correo): se traduce línea a línea y se conservan los saltos y los espacios."""
    if translator(lang) is None:
        return text
    lines = []
    for line in str(text).split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        out = translate_line(stripped, lang)
        lines.append(line[:len(line) - len(line.lstrip())] + out)
    return "\n".join(lines)
