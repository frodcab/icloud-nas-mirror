# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Traducción en el servidor: el gemelo Python del núcleo (core/i18n.py) y los correos con NOTIFY_LANG."""

import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.i18n import Translator, translate_line, translate_text, translator  # noqa: E402

CATALOG = json.loads((APP / "static" / "i18n-en.json").read_text(encoding="utf-8"))
TR = Translator(CATALOG)

# (texto tal como lo compone el código, traducción esperada)
CASES = [
    ("Ya hay una sincronización en marcha", "A sync is already running"),
    ("Docs: 3 foto(s) se borrará. Se aplica ahora.", "Docs: 3 photo(s) will be deleted. Applied now."),
    ("/: 2 elemento(s) se recuperará. Se aplicará al terminar la sync en curso.",
     "/: 2 item(s) will be restored. It will be applied when the current sync finishes."),
    ("Has borrado 3 ficheros en el NAS: se borrarían también en iCloud (van a Eliminados recientemente, recuperables 30 días). "
     "Se han borrado 2 fotos en iCloud: se moverían a la papelera del NAS.",
     "You deleted 3 files on the NAS: they would also be deleted in iCloud (they go to Recently Deleted, recoverable for 30 days). "
     "2 photos were deleted in iCloud: they would be moved to the NAS trash."),
    ("5 ficheros y 2 carpetas borrados esperan tu confirmación en el panel (4 en iCloud, 3 en el NAS); el resto se sincroniza",
     "5 files and 2 folders deleted are waiting for your confirmation in the panel (4 in iCloud, 3 on the NAS); the rest keeps syncing"),
    ("Sesiones cerradas: 3.", "Sessions closed: 3."),
    ("La IP 1.2.3.4 ha quedado bloqueada hasta 10/11 11:00 (contraseña; nivel 2). Puedes desbloquearla en la página Cuenta.",
     "IP 1.2.3.4 has been blocked until 10/11 11:00 (password; level 2). You can unblock it on the Account page."),
    ("La vuelta de Fotos lleva 25 min sin dar señales de vida (fase: listing). Se ha pedido parar; si no responde en 5 min se reinicia la aplicación. "
     "No se pierde nada: el estado está guardado y la sync continúa.",
     "The Photos run has been 25 min without any sign of life (phase: listing). A stop was requested; if it does not answer within 5 min the application restarts. "
     "Nothing is lost: the state is saved and the sync continues."),
    ("borrado en local pero cambiado en iCloud", "deleted locally but changed in iCloud"),
    ("Límite de la vuelta alcanzado: 1000 descargas (4,9 GB)", "Run limit reached: 1000 downloads (4,9 GB)"),
]


@pytest.mark.parametrize("source,expected", CASES)
def test_traduccion_de_mensajes_del_servidor(source, expected):
    assert TR.tr(source) == expected


def test_lo_desconocido_se_deja_igual():
    assert TR.tr("Esto no está en el catálogo") is None
    assert translate_line("Esto no está en el catálogo", "en") == "Esto no está en el catálogo"
    assert translate_line("Sesión cerrada", "es") == "Sesión cerrada"                 # el idioma de origen no toca nada
    assert translate_line("Sesión cerrada", "fr") == "Sesión cerrada"                 # idioma sin catálogo: tampoco


def test_cuerpo_de_correo_linea_a_linea_conservando_formato():
    body = ("Código: 123456\n\nSirve durante 15 minutos para elegir una contraseña nueva en iCloud ⇄ NAS. Lo ha pedido la IP 1.2.3.4.\n\n"
            "Si no has sido tú, ignora este correo: sin tu app de autenticación nadie puede entrar aunque cambie la contraseña.")
    out = translate_text(body, "en")
    assert out.splitlines()[0] == "Code: 123456"
    assert out.count("\n") == body.count("\n")                                         # los saltos de línea se conservan
    assert "It is valid for 15 minutes to choose a new password in iCloud ⇄ NAS. It was requested by IP 1.2.3.4." in out
    assert out.endswith("even if the password is changed.")


def test_el_catalogo_se_carga_una_sola_vez_y_solo_si_hay_idioma():
    assert translator("es") is None and translator("") is None and translator("zz") is None
    assert translator("en") is translator("en")


def test_el_correo_sale_en_el_idioma_configurado(monkeypatch):
    from services import notify
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for lang, subject, line in (("en", "[iCloud sync] Photos: the sync has failed", "You will not be notified again until it recovers"),
                                ("es", "[iCloud sync] Fotos: la sincronización ha fallado", "No se volverá a avisar hasta que se recupere")):
        sent.clear()
        n = notify.EmailNotifier("smtp", 587, "u@example.com", "pw", "to@example.com", lang)
        assert n.send("Fotos: la sincronización ha fallado", "Algo ha pasado\n\nNo se volverá a avisar hasta que se recupere.") is True
        msg = sent[0]
        assert msg["Subject"] == subject
        assert line in msg.get_content()
