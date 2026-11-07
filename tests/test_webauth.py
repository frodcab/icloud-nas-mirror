# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Login autocontenido (core/webauth): contraseña, TOTP, sesiones, bloqueos de IP, anti-CSRF."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pyotp = pytest.importorskip("pyotp")
pytest.importorskip("argon2")
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.webauth import WebAuth  # noqa: E402
from core.webauth.store import BAN_STEPS  # noqa: E402

PASSWORD = "una contraseña larga"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def build(tmp_path, clock, **kw):
    pages = tmp_path / "pages"
    pages.mkdir(exist_ok=True)
    (pages / "login.html").write_text("LOGIN")
    (pages / "cuenta.html").write_text("CUENTA")
    notes = []
    opts = dict(users_file=tmp_path / "config" / "usuarios.json", db_path=tmp_path / "auth.db",
                secret_key="clave-secreta-de-pruebas-123", app_name="Prueba", login_page=pages / "login.html",
                account_page=pages / "cuenta.html", notify=lambda s, b: notes.append(s),
                trusted_proxies=["testclient"], allow_networks=["192.168.0.0/16"], clock=clock)
    opts.update(kw)
    auth = WebAuth(**opts)
    app = FastAPI()
    auth.install(app)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/secreto", dependencies=[Depends(auth.require_user)])
    def secreto():
        return {"dato": 42}

    @app.post("/api/accion", dependencies=[Depends(auth.require_user)])
    def accion():
        return {"hecho": True}

    @app.get("/")
    def index(request: Request):
        return auth.page_or_login(request, pages / "login.html")

    auth.users.bootstrap("alice", PASSWORD)
    return auth, app, notes


def client(app, ip="203.0.113.7"):
    c = TestClient(app)
    c.headers.update({"X-Forwarded-For": ip, "Origin": "http://testserver", "X-Requested-With": "fetch"})
    return c


def login(c, auth, clock, password=PASSWORD, setup=True):
    r = c.post("/auth/login", json={"user": "alice", "password": password})
    if r.status_code != 200:
        return r, None
    if r.json()["next"] == "setup":
        secret = c.get("/auth/setup").json()["secret"]
        r = c.post("/auth/setup", json={"code": pyotp.TOTP(secret).at(clock.t)})
        return r, secret
    return r, None


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    auth, app, notes = build(tmp_path, clock)
    return auth, app, notes, clock, tmp_path


def test_users_file_has_only_hash(env):
    auth, app, notes, clock, tmp = env
    raw = (tmp / "config" / "usuarios.json").read_text()
    data = json.loads(raw)
    assert PASSWORD not in raw and data["usuarios"]["alice"]["hash"].startswith("$argon2id")
    assert auth.users.bootstrap("alice", "otra contraseña cualquiera") is False   # no se pisa


def test_full_flow_setup_totp_then_protected_api(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    assert c.get("/api/secreto").status_code == 401
    r, secret = login(c, auth, clock)
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    assert c.get("/api/secreto").json() == {"dato": 42}
    stored = json.loads((tmp / "config" / "usuarios.json").read_text())["usuarios"]["alice"]
    assert stored["totp"].startswith("v1:") and secret not in json.dumps(stored)   # TOTP cifrado
    assert any("Segundo factor activado" in n for n in notes)
    assert any("Nuevo acceso" in n for n in notes)


def test_wrong_password_generic_message(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    for user in ("alice", "no-existe"):
        r = c.post("/auth/login", json={"user": user, "password": "mala contraseña 123"})
        assert r.status_code in (401, 429)
        if r.status_code == 401:
            assert r.json()["message"] == "Usuario o contraseña incorrectos"


def _second_login(c, clock, code):
    c.post("/auth/logout")
    r = c.post("/auth/login", json={"user": "alice", "password": PASSWORD})
    assert r.json()["next"] == "2fa"
    return c.post("/auth/2fa", json={"code": code})


def test_totp_replay_and_window(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    _, secret = login(c, auth, clock)
    clock.t += 90
    code = pyotp.TOTP(secret).at(clock.t)
    assert _second_login(c, clock, code).status_code == 200
    clock.t += 5
    assert _second_login(c, clock, code).status_code == 401            # mismo código: rechazado
    clock.t += 200
    assert _second_login(c, clock, pyotp.TOTP(secret).at(clock.t - 120)).status_code in (401, 429)  # fuera de ventana


def test_recovery_code_single_use(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    r, secret = login(c, auth, clock)
    code = r.json()["recovery_codes"][0]
    assert _second_login(c, clock, code).status_code == 200
    clock.t += 60
    assert _second_login(c, clock, code).status_code == 401
    assert auth.users.recovery_left("alice") == 9
    assert any("recuperación" in n for n in notes)


def test_session_id_rotates_and_logout(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    c.post("/auth/login", json={"user": "alice", "password": PASSWORD})
    first = c.cookies.get("sesion")
    secret = c.get("/auth/setup").json()["secret"]
    c.post("/auth/setup", json={"code": pyotp.TOTP(secret).at(clock.t)})
    assert c.cookies.get("sesion") != first                         # id nuevo tras el 2º factor
    assert auth.store.get_session(first) is None
    c.post("/auth/logout")
    assert c.get("/api/secreto").status_code == 401


def test_idle_expiry(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    login(c, auth, clock)
    clock.t += 11 * 3600
    assert c.get("/api/secreto").status_code == 200                  # dentro de las 12 h (y renueva)
    clock.t += 13 * 3600
    assert c.get("/api/secreto").status_code == 401


def test_ip_ban_is_progressive_blocks_everything_but_health(env):
    auth, app, notes, clock, tmp = env
    c = client(app, ip="198.51.100.9")
    for i in range(5):
        clock.t += 31                                                # esquiva la espera por usuario
        c.post("/auth/login", json={"user": f"u{i}", "password": "mala contraseña 123"})
    assert c.get("/api/secreto").status_code == 403
    assert c.post("/auth/login", json={"user": "alice", "password": PASSWORD}).status_code == 403
    assert c.get("/health").status_code == 200
    assert any("198.51.100.9 bloqueada" in n for n in notes)
    first = auth.store.list_bans()[0]
    assert first["until"] - clock.t == pytest.approx(BAN_STEPS[0], abs=200)
    clock.t = first["until"] + 1                                     # caduca...
    for i in range(5):                                               # ...y reincide: sube de nivel
        clock.t += 31
        c.post("/auth/login", json={"user": f"v{i}", "password": "mala contraseña 123"})
    second = auth.store.list_bans()[0]
    assert second["level"] == 1 and second["until"] - clock.t == pytest.approx(BAN_STEPS[1], abs=200)


def test_ban_survives_restart(env):
    auth, app, notes, clock, tmp = env
    auth.store.ban("198.51.100.20", "prueba")
    auth2, app2, _ = build(tmp, clock)
    assert client(app2, ip="198.51.100.20").get("/api/secreto").status_code == 403


def test_allowlisted_ip_never_banned(env):
    auth, app, notes, clock, tmp = env
    c = client(app, ip="192.168.7.50")
    for i in range(8):
        clock.t += 31
        c.post("/auth/login", json={"user": f"u{i}", "password": "mala contraseña 123"})
    assert auth.store.list_bans() == []
    assert c.post("/auth/login", json={"user": "alice", "password": PASSWORD}).status_code == 200


def test_forwarded_for_ignored_from_untrusted_peer(tmp_path):
    clock = Clock()
    auth, app, notes = build(tmp_path, clock, trusted_proxies=["10.0.0.1/32"])
    c = client(app, ip="192.168.7.50")             # intenta hacerse pasar por la LAN
    for i in range(5):
        clock.t += 31
        c.post("/auth/login", json={"user": f"u{i}", "password": "mala contraseña 123"})
    bans = auth.store.list_bans()
    assert bans and bans[0]["ip"] == "testclient"  # se bloquea la conexión real, no la IP falsa


def test_only_and_deny_networks(tmp_path):
    clock = Clock()
    auth, app, _ = build(tmp_path, clock, only_networks=["10.8.0.0/24"], deny_networks=["203.0.113.0/24"])
    assert client(app, ip="198.51.100.1").get("/login").status_code == 403    # fuera de las permitidas
    assert client(app, ip="203.0.113.5").get("/login").status_code == 403     # denegada
    assert client(app, ip="10.8.0.5").get("/login").status_code == 200
    assert client(app, ip="192.168.7.2").get("/login").status_code == 200     # lista blanca


def test_manual_ban_and_unban(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    login(c, auth, clock)
    assert c.post("/auth/bans", json={"ip": "198.51.100.77"}).json()["ok"]
    assert client(app, ip="198.51.100.77").get("/login").status_code == 403
    assert c.post("/auth/bans", json={"ip": "203.0.113.7"}).status_code == 400   # la propia no
    c.post("/auth/bans", json={"ip": "198.51.100.77", "action": "unban"})
    assert client(app, ip="198.51.100.77").get("/login").status_code == 200


def test_csrf_origin_and_header_required(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    login(c, auth, clock)
    assert c.post("/api/accion").json() == {"hecho": True}
    assert c.post("/api/accion", headers={"Origin": "https://malo.example"}).status_code == 403
    c2 = client(app)
    c2.cookies = c.cookies
    del c2.headers["X-Requested-With"]
    assert c2.post("/api/accion").status_code == 403


def test_page_redirects_to_login(env):
    auth, app, notes, clock, tmp = env
    r = client(app).get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


def test_broken_or_plaintext_users_file_locks_everyone_out(env):
    auth, app, notes, clock, tmp = env
    f = tmp / "config" / "usuarios.json"
    f.write_text(json.dumps({"usuarios": {"alice": {"hash": PASSWORD}}}))
    r = client(app).post("/auth/login", json={"user": "alice", "password": PASSWORD})
    assert r.status_code == 503
    assert json.loads(f.read_text())["usuarios"]["alice"]["hash"] == PASSWORD  # no se sobrescribe


def test_deactivating_user_kicks_sessions_without_restart(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    login(c, auth, clock)
    assert c.get("/api/secreto").status_code == 200
    import os
    auth.users.set_active("alice", False)
    os.utime(tmp / "config" / "usuarios.json", (clock.t + 5, clock.t + 5))
    assert c.get("/api/secreto").status_code == 401


def test_revoke_other_sessions(env):
    auth, app, notes, clock, tmp = env
    a, b = client(app), client(app, ip="203.0.113.8")
    _, secret = login(a, auth, clock)
    clock.t += 60
    b.post("/auth/login", json={"user": "alice", "password": PASSWORD})
    b.post("/auth/2fa", json={"code": pyotp.TOTP(secret).at(clock.t)})
    assert b.get("/api/secreto").status_code == 200
    assert a.post("/auth/sessions/revoke", json={"all": True}).json()["ok"]
    assert b.get("/api/secreto").status_code == 401 and a.get("/api/secreto").status_code == 200


def test_cli_creates_hashed_user(tmp_path, monkeypatch):
    from core.webauth import __main__ as cli
    f = tmp_path / "u.json"
    monkeypatch.setenv("AUTH_USERS_FILE", str(f))
    monkeypatch.setenv("AUTH_SECRET_KEY", "clave-secreta-de-pruebas-123")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "contraseña muy larga 1")
    cli.main(["usuario-nuevo", "ana"])
    data = json.loads(f.read_text())
    assert data["usuarios"]["ana"]["hash"].startswith("$argon2id") and "contraseña muy larga" not in f.read_text()


def test_default_gateway_parsing(tmp_path):
    from core.webauth.throttle import default_gateway
    f = tmp_path / "route"
    f.write_text("Iface\tDestination\tGateway\tFlags\n"
                 "eth0\t00000000\t0130A8C0\t0003\n"          # 192.168.48.1 en little-endian
                 "eth0\t0030A8C0\t00000000\t0001\n")
    assert default_gateway(str(f)) == "192.168.48.1"
    assert default_gateway(str(tmp_path / "no-existe")) is None


def test_https_via_trusted_proxy_sets_secure_cookie(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    c.headers["X-Forwarded-Proto"] = "https"
    r = c.post("/auth/login", json={"user": "alice", "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-sesion=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()
    assert "Strict-Transport-Security" in r.headers


def test_qr_svg_scales_instead_of_cropping():
    from core.webauth.totp import qr_svg
    svg = qr_svg("otpauth://totp/Prueba:alice?secret=ABCDEFGHIJKLMNOP&issuer=Prueba")
    assert "viewBox" in svg and 'width="' not in svg.split(">", 1)[0]


def test_reset_totp_requires_password_and_code_then_forces_new_setup(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    _, secret = login(c, auth, clock)
    clock.t += 60
    other = client(app, ip="203.0.113.9")
    other.post("/auth/login", json={"user": "alice", "password": PASSWORD})
    other.post("/auth/2fa", json={"code": pyotp.TOTP(secret).at(clock.t)})
    assert other.get("/api/secreto").status_code == 200
    clock.t += 60
    bad = c.post("/auth/totp/reset", json={"password": "mala contraseña 123", "code": pyotp.TOTP(secret).at(clock.t)})
    assert bad.status_code == 401 and auth.users.totp_secret("alice") == secret
    r = c.post("/auth/totp/reset", json={"password": PASSWORD, "code": pyotp.TOTP(secret).at(clock.t)})
    assert r.status_code == 200 and r.json()["next"].startswith("/login")
    assert auth.users.totp_secret("alice") is None and auth.users.recovery_left("alice") == 0
    assert c.get("/api/secreto").status_code == 401                 # esta sesión queda a medias
    assert other.get("/api/secreto").status_code == 401             # las demás, cerradas
    assert c.get("/auth/state").json()["needs_totp_setup"] is True
    new = c.get("/auth/setup").json()["secret"]
    assert new != secret
    assert c.post("/auth/setup", json={"code": pyotp.TOTP(new).at(clock.t)}).status_code == 200
    assert c.get("/api/secreto").status_code == 200
    assert any("restablecido" in n for n in notes)


def test_reset_totp_with_recovery_code_when_phone_is_lost(env):
    auth, app, notes, clock, tmp = env
    c = client(app)
    r, secret = login(c, auth, clock)
    code = r.json()["recovery_codes"][3]
    assert c.post("/auth/totp/reset", json={"password": PASSWORD, "code": code}).status_code == 200
    assert auth.users.totp_secret("alice") is None


# --- ¿has olvidado la contraseña? (código por email) ----------------------------------------------

@pytest.fixture
def mail_env(tmp_path):
    clock = Clock()
    mails = []
    auth, app, notes = build(tmp_path, clock, send_email=lambda s, b, to=None: mails.append((s, b, to)) or True)
    return auth, app, notes, clock, mails


def _code_from(mails):
    import re
    return re.search(r"Código: (\d{6})", mails[-1][1]).group(1)


def test_forgot_password_email_code_changes_password_but_keeps_totp(mail_env):
    auth, app, notes, clock, mails = mail_env
    c = client(app)
    _, secret = login(c, auth, clock)
    other = client(app, ip="203.0.113.9")
    r = other.post("/auth/forgot", json={"user": "alice"})
    assert r.status_code == 200 and len(mails) == 1
    code = _code_from(mails)
    r = other.post("/auth/forgot/confirm", json={"user": "alice", "code": code, "new": "otra contraseña nueva 1"})
    assert r.status_code == 200
    assert c.get("/api/secreto").status_code == 401                        # sesiones cerradas
    assert auth.users.totp_secret("alice") == secret                         # el 2º factor no cambia
    r = other.post("/auth/login", json={"user": "alice", "password": "otra contraseña nueva 1"})
    assert r.json()["next"] == "2fa"                                       # sigue pidiendo el código
    assert any("restablecida" in n for n in notes)
    # el código ya no vale una segunda vez
    assert other.post("/auth/forgot/confirm", json={"user": "alice", "code": code, "new": "y otra más larga 22"}).status_code == 401


def test_forgot_does_not_reveal_users_and_is_rate_limited(mail_env):
    auth, app, notes, clock, mails = mail_env
    c = client(app, ip="192.168.7.4")                      # lista blanca: los fallos no la bloquean
    a = c.post("/auth/forgot", json={"user": "no-existe"})
    b = c.post("/auth/forgot", json={"user": "alice"})
    assert a.json() == b.json() and len(mails) == 1          # misma respuesta; solo el real recibe correo
    c.post("/auth/forgot", json={"user": "alice"})             # antes de 1 minuto: no se envía otro
    assert len(mails) == 1
    clock.t += 61
    c.post("/auth/forgot", json={"user": "alice"})
    assert len(mails) == 2


def test_forgot_code_expires_and_locks_after_5_attempts(mail_env):
    auth, app, notes, clock, mails = mail_env
    c = client(app, ip="192.168.7.4")
    c.post("/auth/forgot", json={"user": "alice"})
    code = _code_from(mails)
    for _ in range(5):
        assert c.post("/auth/forgot/confirm", json={"user": "alice", "code": "000000", "new": "contraseña nueva 123"}).status_code == 401
    assert c.post("/auth/forgot/confirm", json={"user": "alice", "code": code, "new": "contraseña nueva 123"}).status_code == 401
    clock.t += 61
    c.post("/auth/forgot", json={"user": "alice"})
    code = _code_from(mails)
    clock.t += 16 * 60                                        # caducado
    assert c.post("/auth/forgot/confirm", json={"user": "alice", "code": code, "new": "contraseña nueva 123"}).status_code == 401


def test_forgot_wrong_codes_ban_ip(mail_env):
    auth, app, notes, clock, mails = mail_env
    c = client(app, ip="198.51.100.40")
    for i in range(5):
        c.post("/auth/forgot/confirm", json={"user": "alice", "code": f"{i:06d}", "new": "contraseña nueva 123"})
    assert c.get("/login").status_code == 403


def test_forgot_disabled_without_email(env):
    auth, app, notes, clock, tmp = env
    assert client(app).post("/auth/forgot", json={"user": "alice"}).status_code == 503
