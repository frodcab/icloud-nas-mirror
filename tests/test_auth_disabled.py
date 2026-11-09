# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Panel SIN login (AUTH_DISABLED): para quien ya lo protege con su VPN o su proxy.

Lo que debe cumplirse: entra sin sesión SOLO desde redes privadas (o las que se fijen), el anti-CSRF y la comprobación
de Host siguen activos, las rutas de cuenta desaparecen, y el modo normal sigue exigiendo sesión.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("argon2")
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.webauth import WebAuth  # noqa: E402
from core.webauth.throttle import is_private_ip  # noqa: E402


def make(tmp_path, **kw):
    pages = tmp_path / "pages"
    pages.mkdir(exist_ok=True)
    (pages / "login.html").write_text("LOGIN")
    (pages / "index.html").write_text("PANEL")
    opts = dict(users_file=tmp_path / "config" / "usuarios.json", db_path=tmp_path / "auth.db",
                secret_key="clave-secreta-de-pruebas-123", app_name="Prueba", login_page=pages / "login.html",
                trusted_proxies=["testclient"], disabled=True)
    opts.update(kw)
    auth = WebAuth(**opts)
    app = FastAPI()
    auth.install(app)

    @app.get("/api/secreto", dependencies=[Depends(auth.require_user)])
    def secreto():
        return {"dato": 42}

    @app.post("/api/accion", dependencies=[Depends(auth.require_user)])
    def accion():
        return {"hecho": True}

    @app.get("/")
    def index(request: Request):
        return auth.page_or_login(request, pages / "index.html")

    return auth, app


def client(app, ip="192.168.1.20", host=None):
    c = TestClient(app)
    c.headers.update({"X-Forwarded-For": ip, "Origin": "http://testserver", "X-Requested-With": "fetch"})
    if host:
        c.headers["Host"] = host
        c.headers["Origin"] = f"http://{host}"
    return c


def test_sin_sesion_desde_red_privada_entra(tmp_path):
    _, app = make(tmp_path)
    c = client(app)
    assert c.get("/api/secreto").json() == {"dato": 42}
    assert c.post("/api/accion").json() == {"hecho": True}
    assert c.get("/").text == "PANEL"                      # la página se sirve sin redirigir al login


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.20.1.1", "192.168.9.9", "127.0.0.1", "100.101.102.103", "::1", "fd00::5"])
def test_redes_privadas_y_vpn_aceptadas(tmp_path, ip):
    _, app = make(tmp_path)
    assert client(app, ip=ip).get("/api/secreto").status_code == 200


@pytest.mark.parametrize("ip", ["8.8.8.8", "203.0.113.7", "2001:4860:4860::8888", "no-es-una-ip"])
def test_ip_publica_o_desconocida_rechazada(tmp_path, ip):
    _, app = make(tmp_path)
    r = client(app, ip=ip).get("/api/secreto")
    assert r.status_code == 403 and "redes privadas" in r.text


def test_ip_publica_con_aceptacion_explicita(tmp_path):
    _, app = make(tmp_path, allow_public=True)
    assert client(app, ip="8.8.8.8").get("/api/secreto").status_code == 200


def test_only_networks_manda_sobre_lo_privado(tmp_path):
    _, app = make(tmp_path, only_networks=["203.0.113.0/24"], allow_networks=[])
    assert client(app, ip="203.0.113.7").get("/api/secreto").status_code == 200     # pública pero permitida
    assert client(app, ip="192.168.1.20").get("/api/secreto").status_code == 403    # privada pero no listada


def test_el_anti_csrf_sigue_activo(tmp_path):
    _, app = make(tmp_path)
    c = TestClient(app)
    c.headers["X-Forwarded-For"] = "192.168.1.20"
    assert c.post("/api/accion").status_code == 403                                   # sin cabeceras del panel
    assert c.post("/api/accion", headers={"X-Requested-With": "fetch", "Origin": "http://evil.example.com"}).status_code == 403
    assert c.post("/api/accion", headers={"X-Requested-With": "fetch", "Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("host,ok", [
    ("192.168.1.20:8096", True), ("localhost:8096", True), ("nas", True), ("nas.local", True), ("nas.lan:8096", True),
    ("[::1]:8096", True), ("evil.example.com", False), ("rebind.attacker.net:8096", False)])
def test_host_anti_dns_rebinding(tmp_path, host, ok):
    _, app = make(tmp_path)
    r = client(app, host=host).get("/api/secreto")
    assert (r.status_code == 200) is ok
    if not ok:
        assert "AUTH_ALLOWED_HOSTS" in r.text


def test_host_permitido_por_configuracion_y_por_public_url(tmp_path):
    _, app = make(tmp_path, allowed_hosts=["panel.midominio.org"], public_url="https://sync.otro.org")
    assert client(app, host="panel.midominio.org").get("/api/secreto").status_code == 200
    assert client(app, host="sync.otro.org").get("/api/secreto").status_code == 200
    assert client(app, host="evil.example.com").get("/api/secreto").status_code == 403


def test_host_reenviado_por_proxy_de_confianza(tmp_path):
    _, app = make(tmp_path, allowed_hosts=["panel.midominio.org"])
    c = client(app)
    assert c.get("/api/secreto", headers={"X-Forwarded-Host": "panel.midominio.org"}).status_code == 200
    assert c.get("/api/secreto", headers={"X-Forwarded-Host": "evil.example.com"}).status_code == 403


def test_rutas_de_cuenta_desaparecen_y_el_estado_lo_dice(tmp_path):
    _, app = make(tmp_path)
    c = client(app)
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"
    assert c.get("/cuenta", follow_redirects=False).headers["location"] == "/"
    assert c.post("/auth/login", json={"user": "a", "password": "b"}).status_code == 404
    assert c.post("/auth/password", json={}).status_code == 404
    s = c.get("/auth/state").json()
    assert s["auth_disabled"] is True and s["user"] == "local" and s["mfa"] is True


def test_modo_normal_sigue_exigiendo_sesion(tmp_path):
    _, app = make(tmp_path, disabled=False)
    c = client(app, ip="203.0.113.7")
    assert c.get("/api/secreto").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 302
    assert c.get("/auth/state").json().get("auth_disabled") is None


def test_is_private_ip():
    assert is_private_ip("10.1.2.3") and is_private_ip("::ffff:192.168.1.1") and is_private_ip("100.64.0.1")
    assert not is_private_ip("8.8.8.8") and not is_private_ip("100.128.0.1") and not is_private_ip("")
    assert not is_private_ip("203.0.113.7") and not is_private_ip("198.51.100.1")      # rangos de documentación


def _olvidar_config():
    """`from core import config` reutiliza el atributo del paquete aunque se borre de sys.modules: hay que quitarlo
    también, o los demás tests heredan esta configuración (AUTH_DISABLED=true)."""
    import core
    for m in [m for m in sys.modules if m == "main" or m.startswith("core.config")]:
        del sys.modules[m]
    if hasattr(core, "config"):
        delattr(core, "config")


@pytest.fixture(scope="module")
def app_sin_login(tmp_path_factory):
    """La app real (main.py) con AUTH_DISABLED=true: no se crea ningún usuario y el panel va sin sesión."""
    data = tmp_path_factory.mktemp("data")
    (data / "drive").mkdir()
    env = {"DATA_DIR": str(data), "GUI_USER": "alice", "GUI_PASS": "", "ICLOUD_USERNAME": "", "ICLOUD_PASSWORD": "",
           "AUTH_DISABLED": "true", "AUTH_TRUSTED_PROXIES": "testclient", "SMTP_USERNAME": "", "SMTP_PASSWORD": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    _olvidar_config()
    import main
    importlib.reload(main)
    c = TestClient(main.app)
    c.headers.update({"Origin": "http://testserver", "X-Requested-With": "fetch", "X-Forwarded-For": "10.99.1.1"})
    yield main, c, data
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _olvidar_config()


def test_app_real_sin_login(app_sin_login):
    main, c, data = app_sin_login
    assert c.get("/api/status").status_code == 200
    assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200
    assert c.get("/health").json()["auth_disabled"] is True
    assert not (data / "config" / "usuarios.json").exists()                     # no se crea ningún usuario
    assert c.get("/api/status", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
