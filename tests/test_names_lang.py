# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""NAMES_LANG: los nombres de carpetas y sufijos que la app CREA en el disco, separados del idioma de la interfaz.

Con `es` (el valor por defecto, el de las instalaciones existentes) todo queda como siempre; con `en` salen los nombres en inglés y
las bases de datos de estado guardan esas rutas, por lo que el motor tiene que usar el mismo idioma de principio a fin.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import names  # noqa: E402


@pytest.fixture(autouse=True)
def restore_names():
    yield
    names.load("es")


def test_por_defecto_son_los_de_siempre():
    names.load("es")
    assert (names.ALBUMS_DIR, names.HIDDEN_NAME, names.HIDDEN_DIR, names.UNNAMED) == ("Álbumes", "Oculto", "Álbumes/Oculto", "Sin nombre")
    assert names.EDITED_SUFFIX == " (editada)" and names.SUFFIX["edited"] == " (editada)" and names.SUFFIX["original"] == ""
    assert (names.CONFLICT, names.UPLOAD_DIR, names.UPLOADED, names.REJECTED, names.PHOTOS_TRASH) == (
        "conflicto NAS", "Subir", ".subidos", "_no_admitidas", "Fotos")


def test_en_ingles():
    assert names.load("en") == "en"
    assert (names.ALBUMS_DIR, names.HIDDEN_DIR, names.UNNAMED) == ("Albums", "Albums/Hidden", "Unnamed")
    assert names.SUFFIX["edited_live"] == " (edited)" and names.CONFLICT == "NAS conflict"
    assert (names.UPLOAD_DIR, names.UPLOADED, names.REJECTED, names.PHOTOS_TRASH) == ("Upload", ".uploaded", "_rejected", "Photos")


def test_valor_desconocido_vuelve_al_por_defecto(monkeypatch):
    assert names.load("fr") == "es" and names.ALBUMS_DIR == "Álbumes"
    monkeypatch.setenv("NAMES_LANG", " EN ")
    assert names.load() == "en"


def test_el_motor_de_fotos_crea_nombres_en_ingles():
    """Una foto editada, un álbum y el conflicto de Drive salen con los nombres del idioma elegido, de principio a fin."""
    from datetime import datetime, timezone
    import tempfile
    from services.photos_db import PhotosDB
    from services.photos_engine import PhotosEngine
    from services.photos_limits import PhotosLimits
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_photos_engine import FakeRemote, asset
    names.load("en")
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "iCloud"
    root.mkdir()
    d1 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    remote = FakeRemote([asset("a1", "IMG_1.HEIC", d1, live=True, edited="e1")])
    engine = PhotosEngine(PhotosDB(tmp / "photos.db"), root, tmp / "trash", remote, PhotosLimits(pause_s=0, list_pause_s=0), [".DS_Store"], 5,
                          min_age_s=0, sleep=lambda s: None)
    assert engine.upload_dir == "Upload"
    engine.run()
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert "2026/10/IMG_1 (edited).JPG" in files and "2026/10/IMG_1.HEIC" in files
    assert not any("editada" in f or "Álbumes" in f for f in files)
    assert engine.local.trash_prefix == "Photos"


def test_el_conflicto_de_drive_usa_el_idioma_de_los_nombres():
    import inspect
    from services import sync_engine
    src = inspect.getsource(sync_engine)
    assert "names.CONFLICT" in src and '"conflicto NAS' not in src and "(conflicto NAS" not in src.split("class ")[1]


def test_estado_del_panel_publica_los_nombres():
    pytest.importorskip("argon2")
    import importlib
    import tempfile
    from fastapi.testclient import TestClient
    data = Path(tempfile.mkdtemp())
    (data / "drive").mkdir()
    env = {"DATA_DIR": str(data), "NAMES_LANG": "en", "AUTH_DISABLED": "true", "AUTH_TRUSTED_PROXIES": "testclient",
           "ICLOUD_USERNAME": "", "ICLOUD_PASSWORD": "", "SMTP_USERNAME": "", "SMTP_PASSWORD": "", "SYNC_PHOTOS": "true"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    import core
    for m in [m for m in sys.modules if m == "main" or m.startswith("core.config")]:
        del sys.modules[m]
    if hasattr(core, "config"):
        delattr(core, "config")
    try:
        names.load("en")
        import main
        importlib.reload(main)
        c = TestClient(main.app)
        c.headers.update({"X-Forwarded-For": "10.1.1.1", "Origin": "http://testserver", "X-Requested-With": "fetch"})
        assert c.get("/api/status").json()["config"]["conflict_name"] == "NAS conflict"
        cfg = c.get("/api/photos/status").json()["config"]
        assert cfg["albums_dir"] == "Albums" and cfg["edited_name"] == "(edited)" and cfg["upload_dir"] == "Upload"
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
