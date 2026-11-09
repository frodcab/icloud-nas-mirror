# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Integración con FastAPI: middleware (IP, bloqueos, anti-CSRF, cabeceras), rutas y dependencias.

Uso en una app:
    auth = WebAuth(users_file=..., db_path=..., secret_key=..., app_name=..., login_page=..., ...)
    auth.install(app)
    @app.get("/api/algo", dependencies=[Depends(auth.require_user)])
"""

import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from .passwords import hash_password, needs_rehash, verify_password
from .secretbox import SecretBox
from .store import AuthStore
from .throttle import IPPolicy, default_gateway, is_private_ip, register_failure, user_delay
from .totp import new_secret, provisioning_uri, qr_svg, verify
from .users import UsersFileError, UserStore

log = logging.getLogger(__name__)

COOKIE_SECURE = "__Host-sesion"      # por HTTPS: Secure + prefijo __Host- (sin Domain, Path=/)
COOKIE_PLAIN = "sesion"              # en la LAN por HTTP
PENDING_TTL = 10 * 60                # tiempo para completar el 2º factor tras la contraseña
RESET_TTL = 15 * 60                  # validez del código de restablecer contraseña enviado por email
RESET_MAX_ATTEMPTS = 5
RESET_MIN_INTERVAL = 60              # como mucho un código por minuto y usuario...
RESET_MAX_PER_HOUR = 5               # ...y 5 por hora
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
ALWAYS_OPEN = {"/health"}            # ni los bloqueos lo tocan: lo usa el HEALTHCHECK de Docker
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


LOCAL_USER = "local"          # usuario único cuando el panel va sin login (AUTH_DISABLED)


def _host_name(value: str) -> str:
    """Nombre de host de una cabecera Host (sin puerto ni corchetes de IPv6), en minúsculas."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]")[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _plain_403(message: str) -> PlainTextResponse:
    r = PlainTextResponse(message, status_code=403)
    for k, v in SECURITY_HEADERS.items():
        r.headers[k] = v
    return r


class WebAuth:
    def __init__(self, *, users_file: Path, db_path: Path, secret_key: str, app_name: str,
                 login_page: Path, account_page: Path | None = None, notify=None,
                 trusted_proxies=None,
                 allow_networks=("192.168.0.0/16",), deny_networks=(), only_networks=(),
                 public_paths=(), session_idle_hours: float = 12, session_max_days: float = 30,
                 public_url: str = "", send_email=None, clock=time.time,
                 disabled: bool = False, allow_public: bool = False, allowed_hosts=()):
        self.users = UserStore(users_file, SecretBox(secret_key, "totp"))
        self.pending_box = SecretBox(secret_key, "totp-pending")
        self.store = AuthStore(db_path, clock=clock)
        if trusted_proxies is None:
            gw = default_gateway()
            trusted_proxies = ["127.0.0.1/32"] + ([f"{gw}/32"] if gw else [])
        self.trusted_proxies = list(trusted_proxies)
        self.policy = IPPolicy(trusted_proxies, allow_networks, deny_networks, only_networks)
        self.app_name = app_name
        self.login_page, self.account_page = login_page, account_page
        self.notify = notify or (lambda subject, body: None)
        # send_email(subject, body, to) -> bool; sin él no hay "¿has olvidado la contraseña?"
        self.send_email = send_email
        self._reset_key = hashlib.sha256((secret_key + "|password-reset").encode()).digest()
        self.public_paths = set(public_paths) | ALWAYS_OPEN | {"/login"}
        self.idle = session_idle_hours * 3600
        self.max_age = session_max_days * 86400
        self.public_host = urlparse(public_url).netloc if public_url else ""
        # Panel SIN login (AUTH_DISABLED): para quien ya lo protege con su VPN o su proxy. Solo redes privadas salvo
        # que se pida lo contrario, y el anti-CSRF y la comprobación de Host siguen activos.
        self.disabled = disabled
        self.allow_public = allow_public
        self.allowed_hosts = {h.strip().lower() for h in allowed_hosts if h.strip()}
        if self.public_host:
            self.allowed_hosts.add(self.public_host.split(":")[0].lower())
        self.clock = clock
        self.router = self._build_router()

    # --- instalación ----------------------------------------------------------------------------

    def install(self, app) -> None:
        app.middleware("http")(self._middleware)
        app.include_router(self.router)

    async def _middleware(self, request: Request, call_next):
        peer = request.client.host if request.client else ""
        ip = self.policy.client_ip(peer, request.headers.get("x-forwarded-for"))
        request.state.client_ip = ip
        request.state.https = request.url.scheme == "https" or (
            ip != peer or self.policy.is_trusted(peer)) and request.headers.get("x-forwarded-proto") == "https"
        path = request.url.path
        if path in ALWAYS_OPEN:
            return await call_next(request)
        denied = self.policy.denied(ip)
        if denied:
            return _plain_403(f"Acceso denegado: {denied}")
        if self.disabled:
            blocked = self._disabled_gate(request, peer, ip)
            if blocked is not None:
                return blocked
        until = self.store.ban_until(ip)
        if until and not self.policy.allowlisted(ip):
            return _plain_403("IP bloqueada temporalmente por intentos fallidos hasta "
                              + time.strftime("%d/%m %H:%M", time.localtime(until)))
        if request.method in UNSAFE and not self._same_origin(request):
            self._fail(ip, None, "csrf")
            return _plain_403("Petición rechazada (origen no válido)")
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.state.https:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def _disabled_gate(self, request: Request, peer: str, ip: str):
        """Con el login retirado: red de origen, Host (anti DNS rebinding) y rutas de cuenta cerradas."""
        if not self.allow_public and not self.policy.only and not is_private_ip(ip):
            log.warning("Access without authentication rejected from a public IP (%s)", ip)
            return _plain_403("Sin autenticación solo se acepta desde redes privadas. Pon AUTH_ONLY_NETWORKS con las "
                              "redes que quieres permitir (o AUTH_ALLOW_PUBLIC=true bajo tu responsabilidad).")
        fwd = request.headers.get("x-forwarded-host") if self.policy.is_trusted(peer) else None
        host = _host_name(fwd or request.headers.get("host", ""))
        if not self._host_ok(host):
            return _plain_403(f"Host no permitido: «{host}». Añádelo a AUTH_ALLOWED_HOSTS o pon PUBLIC_URL con ese "
                              "nombre (protección contra DNS rebinding).")
        path = request.url.path
        if path in ("/login", "/cuenta"):
            return RedirectResponse("/", status_code=302)
        if path.startswith("/auth/") and path != "/auth/state":
            return JSONResponse({"message": "La autenticación está desactivada (AUTH_DISABLED)."}, 404)
        return None

    def _host_ok(self, host: str) -> bool:
        if not host or host == "localhost" or "." not in host or host in self.allowed_hosts:
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host.endswith((".local", ".lan", ".home.arpa", ".internal", ".localdomain"))

    def _same_origin(self, request: Request) -> bool:
        """Anti-CSRF: la petición debe venir de esta misma web (Origin/Referer) y por fetch del panel."""
        if request.headers.get("x-requested-with") != "fetch":
            return False
        source = request.headers.get("origin") or request.headers.get("referer") or ""
        netloc = urlparse(source).netloc
        allowed = {h for h in (request.headers.get("host"), request.headers.get("x-forwarded-host"),
                               self.public_host) if h}
        return bool(netloc) and netloc in allowed

    # --- sesiones -------------------------------------------------------------------------------

    def _token(self, request: Request) -> str | None:
        return request.cookies.get(COOKIE_SECURE) or request.cookies.get(COOKIE_PLAIN)

    def _session(self, request: Request) -> dict | None:
        s = self.store.get_session(self._token(request) or "")
        now = self.clock()
        if not s:
            return None
        if s["expires"] < now or now - s["created"] > self.max_age or not self.users.is_active(s["user"]):
            self.store.delete_session(s["id_hash"])
            return None
        if s["mfa"]:
            ttl = self.max_age if s["remember"] else self.idle
            self.store.touch_session(s["id_hash"], min(now + ttl, s["created"] + self.max_age))
        return s

    def _set_cookie(self, response, request: Request, token: str, remember: bool) -> None:
        https = getattr(request.state, "https", False)
        response.set_cookie(COOKIE_SECURE if https else COOKIE_PLAIN, token, httponly=True, samesite="strict",
                            secure=https, path="/", max_age=int(self.max_age) if remember else None)

    def _clear_cookie(self, response) -> None:
        for name in (COOKIE_SECURE, COOKIE_PLAIN):
            response.delete_cookie(name, path="/")

    def current_user(self, request: Request) -> str | None:
        if self.disabled:
            return LOCAL_USER
        s = self._session(request)
        return s["user"] if s and s["mfa"] else None

    def require_user(self, request: Request) -> str:
        """Dependencia de FastAPI: sesión con contraseña y TOTP validados."""
        if self.disabled:
            return LOCAL_USER
        s = self._session(request)
        if s and s["mfa"]:
            request.state.session = s
            return s["user"]
        if not self._token(request) and request.url.path.startswith("/api/"):
            # sin ninguna cookie contra la API: escaneo, cuenta para el bloqueo de IP
            self._fail(request.state.client_ip, None, "sin_sesion")
        raise HTTPException(401, "Inicia sesión")

    def page_or_login(self, request: Request, page: Path):
        if self.current_user(request):
            return FileResponse(page, headers={"Cache-Control": "no-store"})
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)

    # --- fallos y avisos ------------------------------------------------------------------------

    def _fail(self, ip: str, user: str | None, kind: str) -> None:
        ban = register_failure(self.store, self.policy, ip, user, kind)
        if ban:
            log.warning("IP %s bloqueada: %s", ip, ban["reason"])
            self.notify(f"IP {ip} bloqueada",
                        f"La IP {ip} ha quedado bloqueada hasta "
                        f"{time.strftime('%d/%m/%Y %H:%M', time.localtime(ban['until']))} "
                        f"({ban['reason']}; nivel {ban['level'] + 1}). Puedes desbloquearla en la página Cuenta.")

    # --- rutas ----------------------------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        r = APIRouter()
        auth = self

        @r.get("/login")
        def login_page():
            return FileResponse(auth.login_page, headers={"Cache-Control": "no-store"})

        @r.get("/auth/state")
        def state(request: Request):
            if auth.disabled:
                return {"app": auth.app_name, "user": LOCAL_USER, "mfa": True, "needs_totp_setup": False,
                        "auth_disabled": True}
            s = auth._session(request)
            try:
                needs_setup = bool(s) and not auth.users.totp_secret(s["user"])
            except UsersFileError:
                needs_setup = False
            return {"app": auth.app_name, "user": s["user"] if s else None, "mfa": bool(s and s["mfa"]),
                    "needs_totp_setup": needs_setup}

        @r.post("/auth/login")
        async def login(request: Request):
            body = await request.json()
            user = str(body.get("user", "")).strip()[:64]
            password = str(body.get("password", ""))
            ip = request.state.client_ip
            wait = user_delay(auth.store, user)
            if wait > 0:
                return JSONResponse({"message": f"Demasiados intentos. Espera {math.ceil(wait)} s."}, 429)
            try:
                u = auth.users.get(user)
            except UsersFileError as exc:
                log.error("%s", exc)
                return JSONResponse({"message": "La configuración de usuarios no es válida: nadie puede "
                                                "entrar hasta corregirla (ver el log)."}, 503)
            ok = verify_password(u["hash"] if u else None, password) and bool(u and u.get("activo", True))
            if not ok:
                auth._fail(ip, user, "password")
                return JSONResponse({"message": "Usuario o contraseña incorrectos"}, 401)
            auth.store.record_attempt(ip, user, True, "password")
            if needs_rehash(u["hash"]):
                auth.users.set_password_hash(user, hash_password(password))
            remember = bool(body.get("remember"))
            token = auth.store.create_session(user, ip, request.headers.get("user-agent", ""),
                                              PENDING_TTL, remember, mfa=False)
            has_totp = bool(u.get("totp"))
            resp = JSONResponse({"next": "2fa" if has_totp else "setup"})
            auth._set_cookie(resp, request, token, remember)
            return resp

        def _pending(request: Request) -> dict:
            s = auth._session(request)
            if not s or s["mfa"]:
                raise HTTPException(401, "La sesión ha caducado; vuelve a introducir la contraseña")
            return s

        def _complete(request: Request, s: dict, body: dict):
            token = auth.store.rotate_session(auth._token(request), mfa=True,
                                              ttl=auth.max_age if s["remember"] else auth.idle)
            ip = request.state.client_ip
            auth.store.record_attempt(ip, s["user"], True, "totp")
            if auth.store.remember_ip(s["user"], ip):
                auth.notify(f"Nuevo acceso de {s['user']} desde {ip}",
                            f"Se ha iniciado sesión en {auth.app_name} desde una IP nueva: {ip}\n"
                            f"Navegador: {request.headers.get('user-agent', '?')}\n"
                            "Si no has sido tú, cierra esa sesión en la página Cuenta y cambia la contraseña.")
            nxt = str(body.get("next") or "/")
            return token, (nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")

        @r.post("/auth/2fa")
        async def second_factor(request: Request):
            s = _pending(request)
            body = await request.json()
            code = str(body.get("code", "")).strip()
            user = s["user"]
            if user_delay(auth.store, user) > 0:
                return JSONResponse({"message": "Demasiados intentos seguidos. Espera unos segundos."}, 429)
            secret = auth.users.totp_secret(user)
            ok = False
            if secret:
                step = verify(secret, code, auth.store.last_totp_step(user), now=auth.clock())
                if step is not None:
                    auth.store.set_totp_step(user, step)
                    ok = True
                elif len(code.replace("-", "")) >= 10 and auth.users.use_recovery_code(user, code):
                    ok = True
                    left = auth.users.recovery_left(user)
                    auth.notify(f"Código de recuperación usado ({user})",
                                f"Se ha entrado con un código de recuperación. Quedan {left}.")
            if not ok:
                auth._fail(request.state.client_ip, user, "totp")
                return JSONResponse({"message": "Código incorrecto o ya usado"}, 401)
            token, nxt = _complete(request, s, body)
            resp = JSONResponse({"ok": True, "next": nxt})
            auth._set_cookie(resp, request, token, s["remember"])
            return resp

        @r.get("/auth/setup")
        def setup_start(request: Request):
            s = _pending(request)
            if auth.users.totp_secret(s["user"]):
                raise HTTPException(409, "El segundo factor ya está activado")
            secret = new_secret()
            auth.store.set_pending_totp(s["id_hash"], auth.pending_box.encrypt(secret))
            uri = provisioning_uri(secret, s["user"], auth.app_name)
            return {"qr_svg": qr_svg(uri), "secret": secret, "uri": uri}

        @r.post("/auth/setup")
        async def setup_finish(request: Request):
            s = _pending(request)
            if not s["pending_totp"] or auth.users.totp_secret(s["user"]):
                raise HTTPException(409, "Vuelve a empezar el alta del segundo factor")
            body = await request.json()
            secret = auth.pending_box.decrypt(s["pending_totp"])
            step = verify(secret, str(body.get("code", "")), -1, now=auth.clock())
            if step is None:
                auth._fail(request.state.client_ip, s["user"], "totp")
                return JSONResponse({"message": "El código no coincide. Revisa la hora del móvil y prueba otra vez."}, 401)
            auth.users.set_totp(s["user"], secret)
            auth.store.set_totp_step(s["user"], step)
            codes = auth.users.new_recovery_codes(s["user"])
            auth.notify(f"Segundo factor activado ({s['user']})",
                        f"Se ha activado el código de verificación (TOTP) para entrar en {auth.app_name}.")
            token, nxt = _complete(request, s, body)
            resp = JSONResponse({"ok": True, "next": nxt, "recovery_codes": codes})
            auth._set_cookie(resp, request, token, s["remember"])
            return resp

        # --- ¿has olvidado la contraseña? (código por email) ----------------------------------
        # Solo cambia la contraseña: el segundo factor sigue haciendo falta para entrar, así que quien
        # tenga acceso a tu correo no entra sin tu app de autenticación.

        def _code_hash(user: str, code: str) -> str:
            return hmac.new(auth._reset_key, f"{user}:{code}".encode(), hashlib.sha256).hexdigest()

        generic = {"ok": True, "message": "Si el usuario existe, te hemos enviado un código por email. "
                                          "Caduca en 15 minutos."}

        @r.post("/auth/forgot")
        async def forgot(request: Request):
            if not auth.send_email:
                return JSONResponse({"message": "El restablecimiento por email no está configurado"}, 503)
            body = await request.json()
            user = str(body.get("user", "")).strip()[:64]
            ip = request.state.client_ip
            try:
                u = auth.users.get(user)
            except UsersFileError:
                u = None
            prev = auth.store.get_password_reset(user) if u else None
            too_soon = prev and auth.clock() - prev["created"] < RESET_MIN_INTERVAL
            if u and u.get("activo", True) and not too_soon and \
                    auth.store.reset_requests_last_hour(user) < RESET_MAX_PER_HOUR:
                code = f"{secrets.randbelow(10 ** 6):06d}"
                auth.store.set_password_reset(user, _code_hash(user, code), RESET_TTL)
                auth.store.record_attempt(ip, user, True, "reset_request")
                auth.send_email(
                    "Código para restablecer la contraseña",
                    f"Código: {code}\n\nSirve durante 15 minutos para elegir una contraseña nueva en "
                    f"{auth.app_name}. Lo ha pedido la IP {ip}.\n\nSi no has sido tú, ignora este correo: "
                    "sin tu app de autenticación nadie puede entrar aunque cambie la contraseña.",
                    u.get("email") or None)
            return generic                   # misma respuesta siempre: no revela qué usuarios existen

        @r.post("/auth/forgot/confirm")
        async def forgot_confirm(request: Request):
            body = await request.json()
            user = str(body.get("user", "")).strip()[:64]
            code = "".join(ch for ch in str(body.get("code", "")) if ch.isdigit())
            ip = request.state.client_ip
            row = auth.store.get_password_reset(user)
            valid = bool(row) and row["expires"] > auth.clock() and row["attempts"] < RESET_MAX_ATTEMPTS \
                and hmac.compare_digest(row["code_hash"], _code_hash(user, code))
            if not valid:
                if row:
                    auth.store.bump_password_reset(user)
                    if row["attempts"] + 1 >= RESET_MAX_ATTEMPTS:
                        auth.store.delete_password_reset(user)   # agotado: hay que pedir otro
                auth._fail(ip, user or None, "reset_code")
                return JSONResponse({"message": "Código incorrecto o caducado"}, 401)
            try:
                auth.users.set_password(user, str(body.get("new", "")))
            except ValueError as exc:
                return JSONResponse({"message": str(exc)}, 400)
            auth.store.delete_password_reset(user)
            closed = auth.store.delete_user_sessions(user)
            auth.store.record_attempt(ip, user, True, "reset_code")
            auth.notify(f"Contraseña restablecida ({user})",
                        f"Se ha cambiado la contraseña de {user} con un código enviado por email, desde la IP "
                        f"{ip}. Sesiones cerradas: {closed}. Si no has sido tú, avisa: el segundo factor "
                        "sigue protegiendo la cuenta.")
            return {"ok": True, "message": "Contraseña cambiada. Ya puedes entrar con ella y tu código."}

        @r.post("/auth/logout")
        def logout(request: Request):
            s = auth.store.get_session(auth._token(request) or "")
            if s:
                auth.store.delete_session(s["id_hash"])
            resp = JSONResponse({"ok": True})
            auth._clear_cookie(resp)
            return resp

        # --- cuenta (requiere sesión completa) --------------------------------------------------

        @r.get("/cuenta")
        def account_page(request: Request):
            return auth.page_or_login(request, auth.account_page)

        @r.get("/auth/account")
        def account(request: Request):
            user = auth.require_user(request)
            me = request.state.session["id_hash"]
            return {
                "user": user,
                "sessions": [{**x, "current": x["id_hash"] == me, "id_hash": None} for x in auth.store.list_sessions(user)],
                "attempts": auth.store.recent_attempts(60),
                "bans": auth.store.list_bans(),
                "recovery_left": auth.users.recovery_left(user),
                "your_ip": request.state.client_ip,
            }

        def _check_totp(user: str, code: str) -> bool:
            secret = auth.users.totp_secret(user)
            step = verify(secret, code, auth.store.last_totp_step(user), now=auth.clock()) if secret else None
            if step is None:
                return False
            auth.store.set_totp_step(user, step)
            return True

        @r.post("/auth/password")
        async def change_password(request: Request):
            user = auth.require_user(request)
            body = await request.json()
            u = auth.users.get(user)
            if not verify_password(u["hash"], str(body.get("current", ""))) or not _check_totp(user, str(body.get("code", ""))):
                auth._fail(request.state.client_ip, user, "password")
                return JSONResponse({"message": "Contraseña actual o código incorrectos"}, 401)
            try:
                auth.users.set_password(user, str(body.get("new", "")))
            except ValueError as exc:
                return JSONResponse({"message": str(exc)}, 400)
            closed = auth.store.delete_user_sessions(user, except_hash=request.state.session["id_hash"])
            auth.notify(f"Contraseña cambiada ({user})", f"Se ha cambiado la contraseña. Sesiones cerradas: {closed}.")
            return {"ok": True, "message": f"Contraseña cambiada. Se han cerrado {closed} sesión(es) más."}

        @r.post("/auth/totp/reset")
        async def reset_totp(request: Request):
            """Restablecer el 2º factor (p.ej. al cambiar de móvil): pide la contraseña actual y un código
            de la app o de recuperación. Borra el TOTP y los códigos, cierra las demás sesiones y deja
            esta a medias para dar de alta el TOTP nuevo en /login."""
            user = auth.require_user(request)
            body = await request.json()
            u = auth.users.get(user)
            code = str(body.get("code", "")).strip()
            # Primero la contraseña: si falla, el código no se gasta (si no, el TOTP quedaría "usado")
            ok = verify_password(u["hash"], str(body.get("password", ""))) and (
                _check_totp(user, code)
                or (len(code.replace("-", "")) >= 10 and auth.users.use_recovery_code(user, code)))
            if not ok:
                auth._fail(request.state.client_ip, user, "password")
                return JSONResponse({"message": "Contraseña o código incorrectos"}, 401)
            auth.users.set_totp(user, None)
            auth.users.clear_recovery_codes(user)
            me = request.state.session["id_hash"]
            closed = auth.store.delete_user_sessions(user, except_hash=me)
            token = auth.store.rotate_session(auth._token(request), mfa=False, ttl=PENDING_TTL)
            auth.notify(f"Segundo factor restablecido ({user})",
                        f"Se ha borrado el código de verificación (TOTP) de {user} desde {request.state.client_ip}. "
                        f"Hay que volver a darlo de alta al entrar. Sesiones cerradas: {closed}.")
            resp = JSONResponse({"ok": True, "next": "/login?next=/cuenta"})
            auth._set_cookie(resp, request, token, request.state.session["remember"])
            return resp

        @r.post("/auth/recovery")
        async def regenerate_recovery(request: Request):
            user = auth.require_user(request)
            body = await request.json()
            if not _check_totp(user, str(body.get("code", ""))):
                auth._fail(request.state.client_ip, user, "totp")
                return JSONResponse({"message": "Código incorrecto"}, 401)
            return {"ok": True, "recovery_codes": auth.users.new_recovery_codes(user)}

        @r.post("/auth/sessions/revoke")
        async def revoke(request: Request):
            user = auth.require_user(request)
            body = await request.json()
            me = request.state.session["id_hash"]
            if body.get("all"):
                n = auth.store.delete_user_sessions(user, except_hash=me)
                return {"ok": True, "message": f"Cerradas {n} sesión(es)"}
            target = [x for x in auth.store.list_sessions(user) if x["id"] == body.get("id") and x["id_hash"] != me]
            for x in target:
                auth.store.delete_session(x["id_hash"])
            return {"ok": bool(target), "message": "Sesión cerrada" if target else "No encontrada"}

        @r.post("/auth/bans")
        async def bans(request: Request):
            auth.require_user(request)
            body = await request.json()
            ip = str(body.get("ip", "")).strip()
            if not ip:
                raise HTTPException(400, "Falta la IP")
            if body.get("action") == "unban":
                return {"ok": auth.store.unban(ip), "message": f"{ip} desbloqueada"}
            if ip == request.state.client_ip or auth.policy.allowlisted(ip):
                return JSONResponse({"message": "No puedes bloquear tu propia IP ni una de la lista blanca"}, 400)
            auth.store.ban(ip, "bloqueo manual", manual=True, seconds=365 * 86400)
            return {"ok": True, "message": f"{ip} bloqueada"}

        return r
