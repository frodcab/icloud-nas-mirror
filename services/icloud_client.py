# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Lado iCloud: sesión de Apple (login + 2FA desde la UI) y operaciones sobre iCloud Drive con pyicloud."""

import logging
import posixpath
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timezone
from pathlib import Path
from typing import Callable

import requests
from pyicloud import PyiCloudService
from pyicloud.const import CONTENT_TYPE_JSON, CONTENT_TYPE_TEXT
from pyicloud.exceptions import (
    PyiCloud2FARequiredException, PyiCloudAPIResponseException, PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException, PyiCloudNoTrustedNumberAvailable,
)
from pyicloud.services.drive import DriveNode

from services.entries import AuthRequired, RemoteEntry
from services.local_fs import in_scope, is_excluded
from services.packages import flatten_package_zip, validate_zip
from services.progress import Progress

log = logging.getLogger(__name__)

AUTH_CODES = {401, 403, 421, 450}          # sesión caducada / hace falta re-login
RETRY_CODES = {429, 500, 502, 503, 504}    # throttling o fallo temporal de Apple
CHUNK = 1024 * 1024


class ICloudAuth:
    """Ciclo de vida de la sesión. Estados: not_configured | disconnected | needs_2fa | ok | error.

    El 2FA va en dos pasos sobre la MISMA instancia de PyiCloudService (request_2fa_code guarda
    en memoria el estado del reto): connect() pide el código y submit_2fa() lo valida.
    """

    def __init__(self, username: str, password: str, session_dir: Path, china: bool,
                 on_change: Callable[[str, str], None] | None = None):
        self.username = username
        self.password = password
        self.session_dir = session_dir
        self.china = china
        self.on_change = on_change
        self.api: PyiCloudService | None = None
        self.state = "not_configured" if not (username and password) else "disconnected"
        self.message = "" if self.state == "disconnected" else "Faltan ICLOUD_USERNAME / ICLOUD_PASSWORD"
        self.delivery = ""
        self._lock = threading.RLock()

    def _set(self, state: str, message: str = "") -> None:
        changed = state != self.state
        self.state, self.message = state, message
        log.info("iCloud session: %s %s", state, message)
        if changed and self.on_change:
            self.on_change(state, message)

    def connect(self) -> str:
        """Login reutilizando las cookies de data/session. Si Apple pide 2FA, lo solicita y espera."""
        with self._lock:
            if self.state == "not_configured":
                return self.state
            self.session_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.api = PyiCloudService(
                    self.username, self.password,
                    cookie_directory=str(self.session_dir),
                    china_mainland=self.china,
                )
                serialize_session_saves(self.api)
            except PyiCloudFailedLoginException as exc:
                # No se reintenta solo: varios fallos seguidos pueden bloquear la cuenta de Apple.
                self.api = None
                self._set("error", f"Login rechazado por Apple (¿contraseña?): {exc}. "
                                   "Corrige ICLOUD_PASSWORD en data/secrets.env, redespliega y pulsa «Reconectar con Apple».")
                return self.state
            except Exception as exc:
                self.api = None
                self._set("disconnected", f"No se pudo conectar con iCloud: {type(exc).__name__}: {exc}")
                return self.state

            if self.api.requires_2fa:
                if getattr(self.api, "security_key_names", None):
                    self._set("error", "La cuenta pide llave de seguridad física (FIDO2): no soportado")
                    return self.state
                try:
                    if not self.api.request_2fa_code():
                        self._set("error", "Apple pide llave de seguridad para este reto: no soportado")
                        return self.state
                except PyiCloudNoTrustedNumberAvailable:
                    self._set("error", "Apple pide 2FA pero la cuenta no tiene teléfono de confianza")
                    return self.state
                except Exception:
                    # Mismo fallback que la CLI de pyicloud: el código que ya muestre el iPhone vale.
                    log.warning("Could not request the 2FA code; the one the device shows will be used",
                                exc_info=True)
                    self.api.use_existing_trusted_device_code()
                self.delivery = getattr(self.api, "two_factor_delivery_method", "") or ""
                self._set("needs_2fa", "Apple ha enviado un código de verificación "
                                       f"({self.delivery or 'dispositivo de confianza'}). Introdúcelo en la web.")
            elif self.api.requires_2sa:
                self._set("error", "La cuenta usa verificación en dos pasos antigua (2SA): no soportado")
            else:
                self._set("ok", "")
            return self.state

    def submit_2fa(self, code: str) -> tuple[bool, str]:
        with self._lock:
            if self.state != "needs_2fa" or self.api is None:
                return False, "No hay ningún código pendiente de validar. Pide un código nuevo."
            try:
                valid, detail = self._validate_code(code.strip())
            except Exception as exc:
                log.exception("Error validating the 2FA code")
                return False, f"Error validando el código: {type(exc).__name__}: {exc}"
            if not valid:
                log.warning("2FA code rejected (%s): %s", self.delivery or "?", detail)
                return False, ("Código incorrecto o caducado. Si has pedido un código nuevo, usa solo el "
                               f"último que te ha llegado. ({detail})")
            if not self.api.is_trusted_session:
                self.api.trust_session()
            if self.api.requires_2fa:
                return False, "Apple ha aceptado el código pero la sesión sigue sin confiar. Pide un código nuevo."
            self._set("ok", "")
            return True, "Sesión verificada"

    def _validate_code(self, code: str) -> tuple[bool, str]:
        """Valida el código 2FA tolerando el cambio de Apple en iOS/macOS 26.4.

        Desde 26.4, cuando el código es correcto Apple puede responder 409 con
        {"securityCode": {"valid": true}}. pyicloud 2.7.0 solo lo acepta en el puente de dispositivo
        de confianza: en SMS y en el endpoint antiguo lo toma como error y validate_2fa_code devuelve
        False sin decir por qué. Por eso esos dos caminos se validan aquí, sobre las mismas
        cabeceras y sesión de pyicloud (depende de internos de 2.7.0, versión fijada en requirements).
        """
        api = self.api
        bridge = getattr(api, "_trusted_device_bridge_state", None)
        method = api.two_factor_delivery_method
        if method != "sms" and bridge is not None and not bridge.uses_legacy_trusted_device_verifier:
            return api.validate_2fa_code(code), "puente de dispositivo de confianza"

        endpoint = api._auth_endpoint
        if method == "sms":
            phone = api._trusted_phone_number()
            if phone is None:
                return False, "la cuenta no tiene teléfono de confianza"
            attempts = [("POST", f"{endpoint}/verify/phone/securitycode", {
                "phoneNumber": phone.as_phone_number_payload(),
                "securityCode": {"code": code},
                "mode": phone.push_mode or "sms",
            })]
            accept = f"{CONTENT_TYPE_JSON}, {CONTENT_TYPE_TEXT}"
        else:
            body = {"securityCode": {"code": code}}
            # 26.4 pasó de POST a PUT: se prueba el de pyicloud y, si Apple no lo admite, el nuevo.
            attempts = [("POST", f"{endpoint}/verify/trusteddevice/securitycode", body),
                        ("PUT", f"{endpoint}/verify/trusteddevice/securitycode", body)]
            accept = CONTENT_TYPE_JSON

        detail = ""
        for verb, url, payload in attempts:
            try:
                api.session.request(verb, url, json=payload,
                                    headers=api._get_auth_headers({"Accept": accept}))
                return self._finish_validation(), f"{verb} OK"
            except PyiCloudAPIResponseException as exc:
                status, body_text = self._error_details(exc)
                if status == 409 and self._says_valid(exc):
                    return self._finish_validation(), f"{verb} 409 válido"
                detail = f"{verb} HTTP {status}: {body_text}"
                if status not in (404, 405):
                    break
        return False, detail

    def _finish_validation(self) -> bool:
        # Lo mismo que hace validate_2fa_code tras un código correcto (trust_session va después).
        self.api._two_factor_code_requested = False
        self.api._clear_trusted_device_bridge_state()
        return True

    @staticmethod
    def _error_details(exc: PyiCloudAPIResponseException) -> tuple[int | None, str]:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) or getattr(exc, "code", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            pass
        text = (getattr(response, "text", "") or str(exc))[:300]
        return status, text

    @staticmethod
    def _says_valid(exc: PyiCloudAPIResponseException) -> bool:
        try:
            data = exc.response.json()
        except Exception:
            return False
        return isinstance(data, dict) and (data.get("securityCode") or {}).get("valid") is True

    def mark_expired(self, reason: str) -> None:
        with self._lock:
            self.api = None
            self._set("disconnected", f"Sesión caducada: {reason}")

    def require_api(self) -> PyiCloudService:
        if self.state != "ok" or self.api is None:
            raise AuthRequired(f"Sesión de iCloud no disponible ({self.state})")
        return self.api


def serialize_session_saves(api) -> None:
    """pyicloud reescribe los ficheros de sesión y cookies tras CADA petición con open("w"). La
    sesión se usa a la vez desde el hilo de sync (con varios hilos al listar) y desde el panel
    (almacenamiento, dispositivos): sin este lock el JSON podría quedar mezclado y habría que
    repetir el 2FA. Idempotente."""
    session = getattr(api, "session", None)
    if session is None or getattr(session, "_icloud_sync_locked", False):
        return
    original, lock = session._save_session_data, threading.Lock()

    def locked_save():
        with lock:
            original()

    session._save_session_data = locked_save
    session._icloud_sync_locked = True


class _NamedReader:
    """Fichero abierto cuyo .name es solo el nombre base: pyicloud lo usa tal cual como nombre en iCloud.

    read/seek/tell tienen que ser métodos de verdad y no delegados con __getattr__: requests detecta
    los ficheros con isinstance(fp, _SupportsRead), un Protocol que desde Python 3.12 usa
    inspect.getattr_static y no ve __getattr__ (en 3.14 las subidas fallaban con TypeError).
    """

    def __init__(self, path: Path, name: str):
        self._f = open(path, "rb")
        self.name = name

    def read(self, size: int = -1) -> bytes:
        return self._f.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._f.seek(offset, whence)

    def tell(self) -> int:
        return self._f.tell()

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _epoch(dt) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt else 0.0


class ICloudDriveRemote:
    """Implementa el protocolo Remote de sync_engine sobre pyicloud.

    Cada run empieza con list_tree(), que guarda el DriveNode de cada ruta para las operaciones siguientes.
    """

    def __init__(self, auth: ICloudAuth, retries: int = 3, progress: Progress | None = None,
                 list_workers: int = 1):
        self.auth = auth
        self.list_workers = max(1, list_workers)
        self.app_libraries: list = []
        self._packages_logged: set[str] = set()
        self.retries = retries
        self.progress = progress
        self._nodes: dict[str, DriveNode] = {}

    def _call(self, fn: Callable, retry: bool = True):
        attempts = self.retries if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except (PyiCloud2FARequiredException, PyiCloudAuthRequiredException) as exc:
                self.auth.mark_expired(str(exc))
                raise AuthRequired(str(exc)) from exc
            except PyiCloudAPIResponseException as exc:
                code = getattr(exc, "code", None)
                code = int(code) if isinstance(code, str) and code.isdigit() else code
                if code in AUTH_CODES:
                    self.auth.mark_expired(f"HTTP {code}")
                    raise AuthRequired(f"HTTP {code}") from exc
                if code not in RETRY_CODES or attempt == attempts:
                    raise
            except requests.RequestException:
                if attempt == attempts:
                    raise
            wait = 2 ** attempt
            log.info("Temporary iCloud error, retry %d/%d in %ds", attempt, attempts - 1, wait)
            time.sleep(wait)

    def _drive(self):
        return self._call(lambda: self.auth.require_api().drive)

    # --- Remote -------------------------------------------------------------------------------

    def list_tree(self, excludes: list[str], folders: list[str]) -> tuple[dict[str, RemoteEntry], list[str]]:
        drive = self._drive()
        self._call(drive.refresh_root)
        root = drive.root
        self._nodes = {"": root}
        self.ignored: set[str] = set()   # el motor lo aparta del plan en los dos lados
        self.app_libraries: list[tuple[str, DriveNode]] = []
        entries: dict[str, RemoteEntry] = {}
        warnings: list[str] = []
        self._serialize_session_saves()
        self._walk([("", root)], excludes, folders, entries, warnings, self._nodes, self.ignored)
        if self.app_libraries:
            # Las App Libraries (Pages, Numbers...) no entran en la sync bidireccional: se copian
            # aparte y solo de iCloud al NAS (ver apps_mirror.py). Un aviso para todas, no uno por app.
            warnings.append("App Libraries fuera de la sync bidireccional (se copian aparte, solo "
                            "iCloud → NAS): " + ", ".join(p for p, _ in self.app_libraries))
        return entries, warnings

    def list_apps(self, excludes: list[str]) -> tuple[dict[str, RemoteEntry], list[str]]:
        """Contenido de todas las App Libraries, con rutas "<App>/...". Requiere list_tree antes."""
        self.app_nodes: dict[str, DriveNode] = {}
        entries: dict[str, RemoteEntry] = {}
        warnings: list[str] = []
        roots = []
        for name, node in self.app_libraries:
            entries[name] = RemoteEntry(name, True, etag=node.data.get("etag"),
                                        drivewsid=node.data.get("drivewsid", ""))
            self.app_nodes[name] = node
            roots.append((name, node))
        ignored: set[str] = set()
        self._walk(roots, excludes, [], entries, warnings, self.app_nodes, ignored)
        self.apps_ignored = ignored
        return entries, warnings

    def _walk(self, roots, excludes, folders, entries, warnings, nodes, ignored) -> None:
        """Recorre en paralelo (LIST_WORKERS) desde roots. Solo la petición de listado va en los
        hilos; entries, nodes e ignored se tocan únicamente en este hilo."""
        with ThreadPoolExecutor(max_workers=self.list_workers, thread_name_prefix="list") as pool:
            pending = {pool.submit(self._call, node.get_children): (base, node) for base, node in roots}
            try:
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        base, _node = pending.pop(future)
                        children = future.result()   # AuthRequired y demás errores se propagan
                        for path, child in self._index_children(base, children, excludes, folders,
                                                                entries, warnings, nodes, ignored):
                            pending[pool.submit(self._call, child.get_children)] = (path, child)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise

    def _serialize_session_saves(self) -> None:
        serialize_session_saves(self.auth.require_api())

    def _index_children(self, base, children, excludes, folders, entries, warnings, nodes, ignored):
        """Registra los hijos de una carpeta y devuelve las subcarpetas que hay que recorrer."""
        if self.progress:
            self.progress.folder_scanned(base, len(children))
        subfolders = []
        for child in children:
            path = _nfc(posixpath.join(base, child.name) if base else child.name)
            if is_excluded(path, excludes) or not in_scope(path, folders):
                continue
            kind = child.type
            if kind == "folder":
                entries[path] = RemoteEntry(
                    path, True, etag=child.data.get("etag"),
                    drivewsid=child.data.get("drivewsid", ""), docwsid=child.data.get("docwsid", ""),
                    zone=child.data.get("zone", ""),
                )
                nodes[path] = child
                subfolders.append((path, child))
            elif kind == "file":
                entries[path] = RemoteEntry(
                    path, False, etag=child.data.get("etag"), size=child.size or 0,
                    mtime=_epoch(child.date_modified),
                    drivewsid=child.data.get("drivewsid", ""), docwsid=child.data.get("docwsid", ""),
                    zone=child.data.get("zone", ""),
                )
                nodes[path] = child
            elif kind == "app_library" and not base:
                ignored.add(path)
                self.app_libraries.append((path, child))
            else:
                ignored.add(path)
                warnings.append(f"Ignorado '{path}': tipo '{kind}' no soportado (paquete)")
        return subfolders

    def download(self, entry: RemoteEntry, dest: Path, nodes: dict | None = None) -> None:
        nodes = self._nodes if nodes is None else nodes
        node = nodes[entry.path]
        if not entry.size:
            dest.write_bytes(b"")
            return
        response, is_package = self._call(lambda: self._open(node))
        written = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(CHUNK):
                f.write(chunk)
                written += len(chunk)
                if self.progress:
                    self.progress.add_bytes(len(chunk))
        if is_package:
            # Documento-paquete (Pages, Numbers, Keynote): iCloud anuncia el tamaño de la carpeta sin
            # comprimir pero lo entrega en zip, así que el tamaño nunca cuadra. Se valida el zip.
            self._check_package_zip(entry.path, dest)
            return
        if written != entry.size:
            # Casi siempre es que el fichero cambió en iCloud desde el listado (en la primera sync
            # pueden pasar horas): se relee y, si ahora cuadra, vale la descarga con los datos nuevos.
            parent = nodes.get(posixpath.dirname(entry.path))
            fresh = self._find_child(parent, posixpath.basename(entry.path)) if parent else None
            if fresh is None or (fresh.size or 0) != written:
                raise IOError(f"el tamaño no cuadra ({written} bytes descargados, iCloud dice "
                              f"{entry.size}); se reintenta en la próxima sync")
            log.info("%s changed in iCloud during the sync; the new version is kept", entry.path)
            entry.size, entry.etag = written, fresh.data.get("etag")
            entry.mtime = _epoch(fresh.date_modified)
            nodes[entry.path] = fresh

    def _open(self, node) -> tuple:
        """Como DriveNode.open(), pero diciendo si Apple lo entrega como paquete (package_token) o
        como fichero normal (data_token): pyicloud lo decide dentro de get_file y no lo expone.
        Depende de internos de pyicloud 2.7.0 (versión fijada)."""
        conn = getattr(node, "connection", None)
        if conn is None or not hasattr(conn, "_document_root"):
            return node.open(stream=True), False          # nodos de test
        params = dict(conn.params)
        params["document_id"] = node.data["docwsid"]
        meta = conn.session.get(f"{conn._document_root}/ws/{node.data['zone']}/download/by_id", params=params)
        info = meta.json()
        data_url = (info.get("data_token") or {}).get("url")
        package_url = (info.get("package_token") or {}).get("url")
        if data_url:
            return conn.session.get(data_url, params=conn.params, stream=True), False
        if package_url:
            return conn.session.get(package_url, params=conn.params, stream=True), True
        raise IOError("iCloud no ha dado enlace de descarga (ni data_token ni package_token)")

    def _check_package_zip(self, path: str, dest: Path) -> None:
        """Valida el zip y lo deja en formato de "archivo único" de iWork (sin la carpeta envolvente),
        para que "X.pages" se abra con doble clic."""
        names = validate_zip(dest)
        flattened = flatten_package_zip(dest, posixpath.basename(path))
        if len(self._packages_logged) < 3:
            self._packages_logged.add(path)
            log.info("Package %s: valid zip with %d entries%s", path, len(names),
                     "; saved as a single iWork file" if flattened else "")

    def upload(self, source: Path, rel_path: str, mtime: float) -> RemoteEntry | None:
        parent_path, name = posixpath.split(rel_path)
        parent = self._nodes[parent_path]
        with _NamedReader(source, name) as f:
            # Sin reintento: si la primera llamada llegó a crear el fichero, repetirla crearía "nombre 2".
            self._call(lambda: parent.upload(f, mtime=mtime, ctime=mtime), retry=False)
        # La subida no devuelve el nodo nuevo: se relee la carpeta para tener su etag.
        for _ in range(3):
            child = self._find_child(parent, name)
            if child is not None:
                self._nodes[rel_path] = child
                return RemoteEntry(
                    rel_path, False, etag=child.data.get("etag"), size=child.size or 0,
                    mtime=_epoch(child.date_modified), drivewsid=child.data.get("drivewsid", ""),
                    docwsid=child.data.get("docwsid", ""), zone=child.data.get("zone", ""),
                )
            time.sleep(2)
        log.warning("Uploaded %s but it does not show in the listing yet; it will be confirmed in the next run", rel_path)
        return None

    def trash(self, entry: RemoteEntry) -> None:
        node = self._nodes.pop(entry.path)
        if entry.is_dir:
            # Tras vaciarla, el etag de la carpeta ha cambiado y Apple rechaza el viejo.
            parent = self._nodes.get(posixpath.dirname(entry.path))
            fresh = self._find_child(parent, posixpath.basename(entry.path)) if parent else None
            node = fresh or node
        self._call(node.move_to_trash)

    def move(self, old_path: str, new_path: str) -> None:
        """Renombra y/o mueve un fichero o carpeta en iCloud y comprueba el resultado.
        Mismo padre: rename. Otro padre: moveItems al destino (que ya debe existir) y, si además
        cambia el nombre, rename."""
        old_parent, old_name = posixpath.split(old_path)
        new_parent, new_name = posixpath.split(new_path)
        node = self._nodes[old_path]
        if old_parent != new_parent:
            dest = self._nodes[new_parent]
            self._call(lambda: node.connection.move_nodes_to_node([node], dest), retry=False)
            moved = self._find_child(dest, old_name)
            if moved is None:
                raise IOError(f"iCloud no muestra '{old_name}' en '{new_parent or '/'}' tras moverlo")
            node = moved
        if old_name != new_name:
            self._call(lambda: node.rename(new_name), retry=False)
        fresh = self._find_child(self._nodes[new_parent], new_name)
        if fresh is None:
            raise IOError(f"iCloud no muestra '{new_path}' tras renombrar/mover")
        for key in [k for k in self._nodes if k == old_path or k.startswith(old_path + "/")]:
            self._nodes[new_path + key[len(old_path):]] = self._nodes.pop(key)
        self._nodes[new_path] = fresh

    def rename_dir(self, old_path: str, new_path: str) -> None:
        """Renombrado pedido a mano (RENOMBRAR.json): solo dentro de la misma carpeta."""
        if posixpath.dirname(old_path) != posixpath.dirname(new_path):
            raise ValueError("solo se renombra dentro de la misma carpeta")
        self.move(old_path, new_path)

    def mkdir(self, rel_path: str) -> RemoteEntry:
        parent_path, name = posixpath.split(rel_path)
        parent = self._nodes[parent_path]
        self._call(lambda: parent.mkdir(name))
        child = self._find_child(parent, name)
        if child is None:
            raise IOError(f"Carpeta {rel_path} creada pero no aparece en iCloud")
        self._nodes[rel_path] = child
        return RemoteEntry(rel_path, True, etag=child.data.get("etag"),
                           drivewsid=child.data.get("drivewsid", ""), docwsid=child.data.get("docwsid", ""),
                           zone=child.data.get("zone", ""))

    def _find_child(self, parent: DriveNode, name: str) -> DriveNode | None:
        target = _nfc(name)
        for child in self._call(lambda: parent.get_children(force=True)):
            if _nfc(child.name) == target:
                return child
        return None
