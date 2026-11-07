# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""La app real (main.py) cargada con datos temporales y SIN credenciales de Apple (no sale a la red).

Cubre que el login protege el panel y que las rutas que usan la sesión de Apple siguen funcionando
(un nombre pisado en main.py las habría roto sin que ningún test unitario lo viera).
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pyotp = pytest.importorskip("pyotp")


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    (data / "drive").mkdir()
    env = {"DATA_DIR": str(data), "GUI_USER": "alice", "GUI_PASS": "contraseña de prueba larga",
           "ICLOUD_USERNAME": "", "ICLOUD_PASSWORD": "", "AUTH_ALLOW_NETWORKS": "10.99.0.0/16",
           "AUTH_TRUSTED_PROXIES": "testclient", "SMTP_USERNAME": "", "SMTP_PASSWORD": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    for m in [m for m in sys.modules if m in ("main",) or m.startswith("core.config")]:
        del sys.modules[m]
    import main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)          # sin "with": no arranca el hilo de sync (lifespan)
    c.headers.update({"Origin": "http://testserver", "X-Requested-With": "fetch", "X-Forwarded-For": "10.99.1.1"})
    yield main, c
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_health_is_public_and_uses_apple_session(app_client):
    main, c = app_client
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["auth_state"] == "not_configured"


def test_panel_requires_login(app_client):
    main, c = app_client
    assert c.get("/api/status").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 302
    assert c.get("/static/app.js").status_code == 401
    assert c.get("/static/login.js").status_code == 200 and c.get("/static/app.css").status_code == 200


def test_login_then_panel_and_apple_routes(app_client):
    main, c = app_client
    assert c.post("/auth/login", json={"user": "alice", "password": "contraseña de prueba larga"}).json()["next"] == "setup"
    secret = c.get("/auth/setup").json()["secret"]
    assert c.post("/auth/setup", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    st = c.get("/api/status").json()
    assert st["auth_state"] == "not_configured" and "config" in st
    r = c.post("/api/auth/2fa", json={"code": "123456"})          # 2FA de APPLE, no del panel
    assert r.status_code == 200 and "pendiente" in r.json()["message"]
    assert c.get("/api/progress").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200


def test_icons_and_manifest_are_public(app_client):
    main, c = app_client
    fresh = main.app and __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
    fresh.headers.update({"X-Forwarded-For": "10.99.2.2"})
    for path in ("/favicon.ico", "/static/icon.svg", "/static/apple-touch-icon.png", "/static/icon-512.png"):
        assert fresh.get(path).status_code == 200, path
    m = fresh.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    assert fresh.get("/static/app.js").status_code == 401            # el panel sigue protegido
