# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Idiomas del panel: el texto en español es la clave y i18n-en.json lo traduce.

Cubre: el núcleo de traducción (con Node), que TODO texto de las páginas HTML tenga traducción, que el catálogo esté sano
(mismos parámetros, nada sin traducir) y que los ficheros del idioma sean públicos (el login los necesita antes de entrar).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
STATIC = APP / "static"
sys.path.insert(0, str(APP))

CATALOG = json.loads((STATIC / "i18n-en.json").read_text(encoding="utf-8"))
norm = lambda s: re.sub(r"\s+", " ", s).strip()                                 # noqa: E731
LETTERS = re.compile(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]{2}")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
TRANSLATABLE_ATTRS = ("placeholder", "title", "aria-label", "alt")


class _Texts(HTMLParser):
    """Textos que ve el usuario en una página: nodos de texto, <title> y atributos placeholder/title/aria-label/alt."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for name, value in attrs:
            if name in TRANSLATABLE_ATTRS and value and LETTERS.search(value):
                self.texts.append(norm(value))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and LETTERS.search(data):
            self.texts.append(norm(data))


def page_texts(name):
    src = (STATIC / name).read_text(encoding="utf-8")
    whole = []
    pattern = re.compile(r"<(\w+)\b[^>]*\bdata-i18n-html\b[^>]*>(.*?)</\1>", re.S)
    for m in pattern.finditer(src):
        whole.append(norm(m.group(2)))                                          # el párrafo entero es una clave
    parser = _Texts()
    parser.feed(pattern.sub("", src))                                           # sin esos párrafos, el resto por nodos
    return parser.texts, whole


# --- el catálogo en sí -------------------------------------------------------------------------------------------

def test_el_catalogo_tiene_los_mismos_parametros_que_sus_claves():
    for key, value in CATALOG.items():
        assert value.strip(), f"traducción vacía: {key!r}"
        assert sorted(PLACEHOLDER.findall(key)) == sorted(PLACEHOLDER.findall(value)), f"parámetros distintos en {key!r}"


def test_no_hay_entradas_repetidas_por_espacios():
    keys = [norm(k) for k in CATALOG]
    assert len(keys) == len(set(keys)), "hay dos claves iguales una vez normalizados los espacios"


# Textos que son iguales en los dos idiomas a propósito (nombres propios, rutas, marcas)
SAME_ON_PURPOSE = {"iCloud ⇄ NAS", "iCloud Drive ⇄", "Log", "Álbumes/", "Cuenta · iCloud sync", "Android", "Mac", "Windows", "Linux"}


def test_nada_queda_sin_traducir_por_olvido():
    same = {k for k, v in CATALOG.items() if norm(k) == norm(v)}
    assert same <= SAME_ON_PURPOSE, f"entradas con la misma traducción que el original (¿sin traducir?): {sorted(same - SAME_ON_PURPOSE)}"


# --- las páginas ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["login.html", "cuenta.html", "index.html"])
def test_todo_texto_de_la_pagina_esta_traducido(page):
    texts, whole = page_texts(page)
    keys = {norm(k) for k in CATALOG}
    missing = sorted({t for t in texts + whole if t not in keys})
    # lo que no lleva letras españolas ni inglesas propias (marcas, #colores…) ya se filtró: lo que queda es texto de verdad
    ignorable = {"width=device-width, initial-scale=1, viewport-fit=cover", "light dark", "noindex, nofollow", "iCloud NAS",
                 "black-translucent", "yes"}
    assert not [m for m in missing if m not in ignorable], f"{page}: textos sin traducción en i18n-en.json: {missing}"


def test_todas_las_paginas_cargan_el_idioma_antes_que_el_resto():
    for page in ("login.html", "cuenta.html", "index.html"):
        src = (STATIC / page).read_text(encoding="utf-8")
        assert src.index("/static/i18n-core.js") < src.index("/static/i18n.js") < src.index("</head>"), page


# --- los textos que pinta el JavaScript ----------------------------------------------------------------------

JS_ES = re.compile(r"[áéíóúñ¿¡…«»]|\b(el|la|los|las|de|del|que|no|se|para|con|una|un|por|en|sin|hay|aún|todo|todas|todos|error|sesión|"
                   r"sincro|fichero|foto|fotos|carpeta|borr|descarg|sub|esper|cargando|actualiz|código|pulsa|puedes|desde|hasta|"
                   r"como|más|ya|solo|si|tu|tus|apple|icloud|nas|sonar|última|próxima|revisado|copia|nada|vez|días|min|archivos|"
                   r"cuenta|hacer|pedir|parar|ver|mostrar|ocultar|dispositivo|dispositivos|conflicto|conflictos|revisar|log|vuelta)\b", re.I)
# Lo que sale en la interfaz sin traducción a propósito: métodos HTTP, navegadores y sistemas, y unidades iguales en los dos idiomas
JS_IGNORE = {"get", "post", "android", "mac", "windows", "linux", "edge", "chrome", "firefox", "safari", "error {}", "hace x min",
             "{} h {} min", "{} min {}", "{} min"}
shape = lambda s: re.sub(r"\{\w+\}", "{}", norm(s))                                      # noqa: E731


def js_texts(name):
    """Textos visibles que escribe un .js: literales y el texto entre etiquetas de las plantillas, con ${x} -> {x}."""
    src = (STATIC / name).read_text(encoding="utf-8")
    out = set()
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`|\'((?:[^\'\\\n]|\\.)*)\'', src):
        s = next(g for g in m.groups() if g is not None)
        if s.startswith(("/", "#", ".")):
            continue
        if " " not in s.strip() and not re.search(r"[áéíóúñ¿¡…]", s) and not re.fullmatch(r"[A-ZÁÉÍÓÚÑ¿¡][A-Za-záéíóúñ]{2,}", s):
            continue
        s2 = re.sub(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", "{x}", s)
        for part in re.split(r"<[^>]+>", s2):
            p = norm(part.replace("\\n", " "))
            if len(p) >= 3 and LETTERS.search(p) and "${" not in p and (JS_ES.search(re.sub(r"\{\w+\}", "", p)) or p[0].isupper()):
                out.add(p)
    return out


@pytest.mark.parametrize("script", ["app.js", "fotos.js", "cuenta.js", "login.js"])
def test_todo_texto_del_js_esta_traducido(script):
    keys = {shape(k) for k in CATALOG}
    missing = sorted(t for t in js_texts(script) if shape(t) not in keys and shape(t).lower() not in JS_IGNORE)
    assert not missing, f"{script}: textos sin traducción en i18n-en.json: {missing}"


# --- el núcleo (JavaScript) con Node ----------------------------------------------------------------------------

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_nucleo_de_traduccion_con_node():
    script = """
    const C = require(process.argv[1]);
    const t = C.makeTranslator({"Hola": "Hello", "{n} fotos": "{n} photos", "Subida {n} de {total}": "Upload {n} of {total}",
                               "Foto {n}": "Pic {n}", "Borrar {n} fichero(s) de «{dir}»": "Delete {n} file(s) from “{dir}”",
                               "Si pierdes <b>algo</b>": "If you lose <b>something</b>"});
    const out = {
      exacto: t.tr("Hola"), espacios: t.tr("  Hola \\n "), plantilla: t.tr("3 fotos"), dos: t.tr("Subida 2 de 10"),
      comillas: t.tr("Borrar 4 fichero(s) de «/Fotos/2026»"), desconocido: t.tr("nada de esto"), vacio: t.tr("   "),
      componer: t.t("{n} fotos", {n: 7}), componerSinCatalogo: t.t("Sin catalogo {x}", {x: 1}), html: t.tr("Si pierdes <b>algo</b>"),
      especifica: t.tr("Foto 9"),
    };
    console.log(JSON.stringify(out));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "i18n-core.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["exacto"] == "Hello" and out["espacios"] == "Hello"
    assert out["plantilla"] == "3 photos" and out["dos"] == "Upload 2 of 10"
    assert out["comillas"] == "Delete 4 file(s) from “/Fotos/2026”"
    assert out["desconocido"] is None and out["vacio"] is None
    assert out["componer"] == "7 photos" and out["componerSinCatalogo"] == "Sin catalogo 1"
    assert out["html"] == "If you lose <b>something</b>" and out["especifica"] == "Pic 9"


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_el_catalogo_real_traduce_con_el_nucleo():
    script = """
    const C = require(process.argv[1]); const cat = require(process.argv[2]);
    const t = C.makeTranslator(cat);
    console.log(JSON.stringify([t.tr("Sincronizar ahora"), t.tr("Cada foto se guarda en"), t.tr("Hola, esto no existe"),
      t.tr("Última sincronización: con errores"), t.tr("12 de 40 operaciones"), t.tr("hace 5 min"), t.tr("última hace unos segundos"),
      t.tr("en marcha desde hace 3 min"), t.tr("Todo sincronizado · simulación"), t.tr("¿Borrar 3 fichero(s) de «Docs» también en iCloud (a Eliminados recientemente, recuperable 30 días)?"),
      t.tr("1.2.3.4 · último uso hace 2 min · caduca en 3 h"), t.tr("contraseña · hasta 10/11/26 11:00 · nivel 2"),
      t.tr("última hace 14 min · próxima en 31 min"), t.tr("Descargado de iCloud · 214.8 KB · 10:31:05")]));
    """
    r = subprocess.run([node, "-e", script, str(STATIC / "i18n-core.js"), str(STATIC / "i18n-en.json")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [
        "Sync now", "Each photo is saved in", None, "Last sync: with errors", "12 of 40 operations", "5 min ago",
        "last run a few seconds ago", "running for 3 min", "All synced · simulation",
        "Delete 3 file(s) from “Docs” also in iCloud (to Recently Deleted, recoverable for 30 days)?",
        "1.2.3.4 · last used 2 min ago · expires in 3 h", "password · until 10/11/26 11:00 · level 2",
        "last run 14 min ago · next run in 31 min", "Downloaded from iCloud · 214.8 KB · 10:31:05"]


@pytest.mark.skipif(node is None or not os.environ.get("JSDOM_DIR"), reason="opcional: necesita Node y JSDOM_DIR (ver tests/js)")
def test_cargador_en_un_dom_simulado():
    r = subprocess.run([node, str(APP / "tests" / "js" / "i18n_dom_check.js")], capture_output=True, text=True, timeout=120,
                       env={**os.environ})
    assert r.returncode == 0, r.stdout + r.stderr


# --- los ficheros públicos ----------------------------------------------------------------------------------------

def test_ficheros_de_idioma_publicos_y_resto_protegido():
    pytest.importorskip("argon2")
    import importlib
    import os
    from fastapi.testclient import TestClient
    data = Path(__import__("tempfile").mkdtemp())
    (data / "drive").mkdir()
    env = {"DATA_DIR": str(data), "GUI_USER": "alice", "GUI_PASS": "contraseña de prueba larga", "ICLOUD_USERNAME": "",
           "ICLOUD_PASSWORD": "", "AUTH_TRUSTED_PROXIES": "testclient", "SMTP_USERNAME": "", "SMTP_PASSWORD": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    import core
    for m in [m for m in sys.modules if m == "main" or m.startswith("core.config")]:
        del sys.modules[m]
    if hasattr(core, "config"):
        delattr(core, "config")
    try:
        import main
        importlib.reload(main)
        c = TestClient(main.app)
        c.headers.update({"X-Forwarded-For": "10.99.2.2"})
        for name in ("i18n.js", "i18n-core.js", "i18n-en.json"):
            assert c.get(f"/static/{name}").status_code == 200, name
        assert c.get("/static/i18n-en.json").json()["Entrar"] == "Sign in"
        assert c.get("/static/app.js").status_code == 401                         # el panel sigue protegido
        assert c.get("/static/i18n-fr.json").status_code == 404                  # idioma que no existe
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for m in [m for m in sys.modules if m == "main" or m.startswith("core.config")]:
            del sys.modules[m]
        if hasattr(core, "config"):
            delattr(core, "config")
