# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Lado iCloud de la sync de Fotos: lista la fototeca, descarga, sube y borra con pyicloud 2.7.0.

Usa la MISMA sesión de Apple que Drive (ICloudAuth). Los frenazos de Apple (429/503 tras los
reintentos) se avisan al presupuesto del run (photos_limits.RunBudget), que corta el run si se
repiten. Depende de internos de pyicloud 2.7.0 (versión fijada en requirements.txt):
- las ediciones del iPhone se leen de los campos resJPEGFull*/resVidFull* del registro CPLAsset,
  que pyicloud pide pero no ofrece como versión;
- la subida usa library._client.upload_file para saber si Apple la tomó por duplicada.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

import requests

from pyicloud.exceptions import PyiCloud2FARequiredException, PyiCloudAPIResponseException, PyiCloudAuthRequiredException
from pyicloud.common.cloudkit.client import CloudKitApiError
from pyicloud.common.cloudkit.models import (CKErrorItem, CKRecord, CKTombstoneRecord, CKZoneChangesRequest,
                                             CKZoneChangesZoneReq, CKZoneID, CKZoneIDReq)
from pyicloud.services.photos_cloudkit import DirectionEnum, PhotoAlbumFolder, SmartPhotoAlbum
from pyicloud.services.photos_cloudkit.service import PHOTO_DESIRED_KEYS
from pyicloud.services.photos_cloudkit.mappers import record_field_value

from services.entries import AuthRequired
from services.photos_limits import RunBudget
from services.progress import Progress

log = logging.getLogger(__name__)

AUTH_CODES = {401, 403, 421, 450}
THROTTLE_CODES = {429, 503}
RETRY_CODES = {429, 500, 502, 503, 504}
CHUNK = 1024 * 1024
# Las URL de descarga de Apple caducan (comprobado: recién leídas dan 206; tras el listado entero de
# la fototeca, ~25 min, todas daban 410). Con más edad que esto, se relee la foto antes de bajarla.
URL_MAX_AGE_S = 600

# Tipo de fichero de Apple → extensión (los de pyicloud, más los de las ediciones)
EXTENSIONS = {
    "public.heic": ".HEIC", "public.heif": ".HEIF", "public.jpeg": ".JPG", "public.png": ".PNG",
    "com.apple.quicktime-movie": ".MOV", "public.mpeg-4": ".MP4", "com.apple.m4v-video": ".M4V",
    "public.tiff": ".TIFF", "com.compuserve.gif": ".GIF", "com.adobe.raw-image": ".DNG",
}


GONE = "gone"            # lookup(): la foto ya no está (borrada o en Eliminados recientemente)
HIDDEN = "hidden"        # lookup(): la foto está en el álbum Oculto
LOOKUP_BATCH = 100       # ids por consulta a Apple (CloudKit admite hasta 200 registros)


class Throttled(Exception):
    """Apple frena (429/503) y ya se agotaron los reintentos de esta operación."""


@dataclass
class RemoteFile:
    role: str            # original | live | edited | edited_live
    name: str            # nombre propuesto (sin carpeta)
    url: str | None
    size: int
    fingerprint: str | None
    listed_at: float = 0.0   # cuándo se leyó la URL (caduca)


@dataclass
class RemoteAsset:
    id: str
    filename: str
    date: datetime       # fecha de la foto (UTC)
    kind: str            # image | movie
    files: dict[str, RemoteFile] = field(default_factory=dict)
    hidden: bool = False


def _token(value) -> tuple[str | None, int]:
    if value is None:
        return None, 0
    if isinstance(value, dict):
        return value.get("downloadURL"), int(value.get("size") or 0)
    return getattr(value, "downloadURL", None), int(getattr(value, "size", 0) or 0)


def _field(record, name):
    try:
        return record_field_value(record, name)
    except Exception:
        return None


def _is_network_error(exc: BaseException) -> bool:
    """Corte de red, también cuando pyicloud lo envuelve en su propia excepción ("Request failed to iCloud",
    con el ConnectionError de requests en __cause__): el 5/11 un RemoteDisconnected así mató una vuelta de 6 h."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (ConnectionError, TimeoutError, requests.exceptions.RequestException)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _status(exc) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        for attr in ("code", "status_code"):
            v = getattr(obj, attr, None)
            if isinstance(v, str) and v.isdigit():
                v = int(v)
            if isinstance(v, int):
                return v
    text = str(exc)
    for code in (*AUTH_CODES, *RETRY_CODES):
        if f"HTTP {code}" in text or f" {code} " in f" {text} ":
            return code
    return None


def describe(asset) -> RemoteAsset:
    """PhotoAsset de pyicloud → RemoteAsset con los ficheros que tocan en el NAS."""
    res = asset.resources
    original = res.get("original")
    name = original.filename if original else asset.filename
    base = os.path.splitext(name)[0]
    ra = RemoteAsset(id=asset.id, filename=name, date=asset.asset_date, kind=asset.item_type)
    if original is not None:
        ra.files["original"] = RemoteFile("original", name, original.url, int(original.size or 0), original.checksum)
    live = res.get("original_video") if asset.is_live_photo else None
    if live is not None:
        ra.files["live"] = RemoteFile("live", base + os.path.splitext(live.filename)[1] if live.filename else base + ".MOV",
                                      live.url, int(live.size or 0), live.checksum)
    # Ediciones del iPhone: viven en el CPLAsset (el CPLMaster es el original intacto)
    rec = asset._asset_record
    for prefix, role in (("resJPEGFull", "edited"), ("resVidFull", "edited_live")):
        url, size = _token(_field(rec, f"{prefix}Res"))
        if not url:
            continue
        if role == "edited_live" and ra.kind == "movie":
            role = "edited"                         # vídeo editado: su edición es el propio vídeo
        ext = EXTENSIONS.get(_field(rec, f"{prefix}FileType") or "", ".JPG" if prefix == "resJPEGFull" else ".MOV")
        ra.files[role] = RemoteFile(role, f"{base} (editada){ext}", url, size, _field(rec, f"{prefix}Fingerprint"))
    return ra


class SlowDownload(IOError):
    """La descarga de un fichero ha pasado de su tope de tiempo (Apple la sirve a goteo)."""


MIN_SPEED_BPS = 200 * 1024       # por debajo de esto un fichero grande no acabaría en un tiempo razonable


def max_download_seconds(size: int, cap_s: float) -> float:
    return min(cap_s, 120 + size / MIN_SPEED_BPS)


class ICloudPhotosRemote:
    def __init__(self, auth, progress: Progress | None = None, retries: int = 3, net_retries: int = 6,
                 sleep: Callable[[float], None] = time.sleep, max_file_s: float = 2700):
        self.max_file_s = max_file_s
        self.auth = auth
        self.progress = progress
        self.retries = retries
        self.net_retries = max(net_retries, retries)   # un corte de red dura más que un frenazo: más intentos
        self.sleep = sleep
        self.budget: RunBudget | None = None
        self._handles: dict[str, object] = {}   # id → PhotoAsset del último listado (para borrar)

    # --- errores y reintentos ---------------------------------------------------------------------

    def _call(self, fn: Callable):
        for attempt in range(1, self.net_retries + 1):
            try:
                if self.progress:
                    self.progress.touch()
                result = fn()
                if self.progress:
                    self.progress.touch()       # la llamada a Apple ha vuelto: la vuelta sigue viva
                return result
            except (PyiCloud2FARequiredException, PyiCloudAuthRequiredException) as exc:
                self.auth.mark_expired(str(exc))
                raise AuthRequired(str(exc)) from exc
            except AuthRequired:
                raise
            except Exception as exc:
                code = _status(exc)
                name = type(exc).__name__
                if code in AUTH_CODES or name == "CloudKitAuthError":
                    self.auth.mark_expired(f"HTTP {code}" if code else name)
                    raise AuthRequired(f"HTTP {code}" if code else name) from exc
                throttle = code in THROTTLE_CODES or name == "CloudKitRateLimited"
                transient = _is_network_error(exc)
                if not (throttle or code in RETRY_CODES) and not transient:
                    raise
                limit = self.net_retries if (transient and not throttle and code not in RETRY_CODES) else self.retries
                if attempt >= limit:
                    if throttle:
                        raise Throttled(f"{name} HTTP {code}") from exc
                    raise
                wait = min(60, 5 * 2 ** (attempt - 1))
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    wait = min(300, retry_after)
                log.info("Apple responde %s; reintento %d/%d en %ds", code or name, attempt, limit - 1, wait)
                self.sleep(wait)

    def _library(self):
        api = self.auth.require_api()
        return self._call(lambda: api.photos.libraries["root"])

    # --- lectura ----------------------------------------------------------------------------------

    def cursor(self) -> str | None:
        """Token de cambios de la fototeca: si no cambia entre dos runs, en iCloud no ha cambiado nada."""
        lib = self._library()
        lib._current_sync_token = None            # sin caché: el valor de ahora
        try:
            return self._call(lib.sync_cursor)
        except AuthRequired:
            raise
        except Exception as exc:
            log.info("No se pudo leer el cursor de cambios de Fotos (%s); se lista entera", exc)
            return None

    def count(self) -> int:
        lib = self._library()
        return int(self._call(lambda: len(lib.all)))

    def list_assets(self, keep: set[str] | frozenset = frozenset()) -> Iterator[RemoteAsset]:
        """Toda la fototeca ("Todas las fotos"), página a página, con pausa entre páginas. Lo que
        está en Eliminados recientemente u Oculto no sale. Del objeto de pyicloud (pesado: decenas de
        miles de registros de CloudKit) solo se guardan los de `keep`, los que se pueden borrar."""
        lib = self._library()
        album = self._call(lambda: lib.all)
        self._handles = {}
        # Mismo paginado que PhotoAlbum.photos (ascendente), pero página a página para poder
        # reintentar una sola página: si falla el generador de pyicloud, ya no se puede seguir.
        page_size = album.page_size
        offset, n, seen = 0, 0, set()
        while True:
            page = self._call(lambda: list(album._get_photos_at(offset, DirectionEnum.ASCENDING, page_size)))
            for asset in page:
                if asset.id in seen:
                    continue
                seen.add(asset.id)
                if asset.id in keep:
                    self._handles[asset.id] = asset
                n += 1
                ra = describe(asset)
                now = time.time()
                for rf in ra.files.values():
                    rf.listed_at = now
                yield ra
            if self.progress:
                self.progress.folder_scanned(f"Fotos: {n} leídas", len(page))
            if len(page) < page_size // 2:
                return
            offset += len(page)
            if self.budget:
                self.budget.page()

    def new_run(self) -> None:
        self._seen_added: set[str] = set()

    def page_newest(self, from_top: int) -> tuple[list[RemoteAsset], int, int, int]:
        """Una página de la más reciente a la más antigua, por fecha en que entró en iCloud (índice ADDED,
        el de "Añadidas recientemente"; el de "Todas las fotos" no se deja recorrer hacia atrás de forma
        fiable: comprobado el 2026-10-25). Igual que PhotoAlbum._iter_added_desc_photos de pyicloud: la
        posición n devuelve las más nuevas hasta n y la ventana llega de la más vieja a la más nueva.
        Devuelve (fotos visibles nuevas, cuánto avanza la posición, nº de elementos que dio Apple, tamaño
        de página). La posición avanza por TODO lo nuevo de la ventana, también ocultas y borradas: con
        ~28.000 ocultas, contar solo las visibles dejaba la carga sin avanzar (2026-10-26)."""
        lib = self._library()
        album = self._call(lambda: lib.recently_added())
        page_size = album.page_size
        offset = from_top + page_size - 1
        window = self._call(lambda: list(album._get_photos_at(offset, DirectionEnum.DESCENDING, page_size)))
        seen = getattr(self, "_seen_added", None)
        if seen is None:
            seen = self._seen_added = set()
        now, out, advance = time.time(), [], 0
        for asset in reversed(window):
            if asset.id in seen:
                continue
            seen.add(asset.id)
            advance += 1
            rec = asset._asset_record
            if _field(rec, "isDeleted") or _field(rec, "isHidden"):
                continue
            ra = describe(asset)
            for rf in ra.files.values():
                rf.listed_at = now
            out.append(ra)
        if self.budget:
            self.budget.page()
        return out, advance, len(window), page_size

    def page_at(self, from_top: int) -> tuple[list[RemoteAsset], int, int, float]:
        """Una página del mismo índice (más recientes primero) en CUALQUIER posición, sin mirar lo ya visto en
        la vuelta y sin tocar el recorrido en orden: para sondas y búsquedas arbitrarias en páginas altas.
        Devuelve (fotos visibles, nº de elementos que dio Apple, tamaño de página, segundos que tardó)."""
        t0 = time.time()
        lib = self._library()
        album = self._call(lambda: lib.recently_added())
        page_size = album.page_size
        offset = from_top + page_size - 1
        window = self._call(lambda: list(album._get_photos_at(offset, DirectionEnum.DESCENDING, page_size)))
        now, out = time.time(), []
        for asset in reversed(window):
            rec = asset._asset_record
            if _field(rec, "isDeleted") or _field(rec, "isHidden"):
                continue
            ra = describe(asset)
            for rf in ra.files.values():
                rf.listed_at = now
            out.append(ra)
        if self.budget:
            self.budget.page()
        return out, len(window), page_size, time.time() - t0

    def lookup(self, asset_ids) -> dict[str, "RemoteAsset | str"]:
        """Fotos por id, en bloques (dos consultas por bloque: CPLAsset y sus CPLMaster). Valor: la foto,
        o GONE si Apple dice que ya no existe, está en Eliminados recientemente u oculta. Las que Apple
        no responde con claridad no salen (ante la duda no se toca nada)."""
        ids = list(dict.fromkeys(asset_ids))
        if not ids:
            return {}
        lib = self._library()
        zone = CKZoneIDReq(**lib.zone_id)
        out: dict[str, RemoteAsset | str] = {}
        hidden: set[str] = set()
        self.hidden_assets: dict[str, RemoteAsset] = {}    # ocultas de esta consulta, con sus URL
        for i in range(0, len(ids), LOOKUP_BATCH):
            chunk = ids[i:i + LOOKUP_BATCH]
            resp = self._call(lambda: lib._client.lookup(record_names=chunk, zone_id=zone,
                                                         desired_keys=PHOTO_DESIRED_KEYS))
            assets, masters = {}, {}
            for rec in resp.records:
                if isinstance(rec, CKTombstoneRecord):
                    out[rec.recordName] = GONE
                elif isinstance(rec, CKErrorItem):
                    if rec.recordName and rec.serverErrorCode == "NOT_FOUND":
                        out[rec.recordName] = GONE
                elif isinstance(rec, CKRecord) and rec.recordType == "CPLAsset":
                    if _field(rec, "isDeleted"):
                        out[rec.recordName] = GONE
                        continue
                    if _field(rec, "isHidden"):
                        hidden.add(rec.recordName)        # se resuelve como las demás (para poder borrarla)
                    ref = _field(rec, "masterRef")
                    master = getattr(ref, "recordName", None) or (ref.get("recordName") if isinstance(ref, dict) else None)
                    if master:
                        assets[rec.recordName] = rec
                        masters[master] = rec.recordName
            if masters:
                mresp = self._call(lambda: lib._client.lookup(record_names=list(masters), zone_id=zone,
                                                              desired_keys=PHOTO_DESIRED_KEYS))
                now = time.time()
                for rec in mresp.records:
                    if isinstance(rec, CKRecord) and rec.recordType == "CPLMaster" and rec.recordName in masters:
                        aid = masters[rec.recordName]
                        pa = lib.asset_type(lib.service, rec, assets[aid], library=lib)
                        self._handles[aid] = pa          # para poder borrarla en iCloud
                        ra = describe(pa)
                        for rf in ra.files.values():
                            rf.listed_at = now
                        if aid in hidden:
                            ra.hidden = True
                            self.hidden_assets[aid] = ra
                            out[aid] = HIDDEN
                        else:
                            out[aid] = ra
            if self.budget:
                self.budget.page()
        return out

    def albums(self) -> list[tuple[str, list[str], object]]:
        """Álbumes que se reflejan en el NAS: (tipo, ruta de carpetas, álbum de pyicloud). Tipo "hidden"
        para Oculto; "album" para los tuyos (con sus carpetas, p.ej. ["Viajes", "Italia"]) y Favoritas.
        El resto de inteligentes (Vídeos, Selfies…) no: son filtros, no álbumes."""
        lib = self._library()
        out = []
        for album in self._call(lambda: list(lib.refresh_albums())):
            if isinstance(album, SmartPhotoAlbum):
                if album.id == "Hidden":
                    out.append(("hidden", ["Oculto"], album))
                elif album.id == "Favorites":
                    out.append(("album", ["Favoritas"], album))
                continue
            if isinstance(album, PhotoAlbumFolder):
                continue
            # Un álbum sin nombre (lo hay: ~6.300 fotos) iría suelto a Álbumes/: se le da una carpeta
            out.append(("album", [p for p in album.fullname.split("/") if p.strip()] or ["Sin nombre"], album))
        return out

    def refresh_urls(self, items: list[tuple[str, RemoteFile]]) -> int:
        """URL nuevas para varias fotos a la vez (por id, en bloques de LOOKUP_BATCH: 2 peticiones por
        bloque). Vale también para las ocultas, que no están en "Todas las fotos"."""
        if not items:
            return 0
        looked = self.lookup([aid for aid, _ in items])
        hidden = getattr(self, "hidden_assets", {})
        now, n = time.time(), 0
        for aid, rf in items:
            ra = looked.get(aid)
            ra = hidden.get(aid) if ra == HIDDEN else ra
            fresh = ra.files.get(rf.role) if isinstance(ra, RemoteAsset) else None
            if fresh is not None and fresh.url:
                rf.url, rf.listed_at = fresh.url, now
                n += 1
        return n

    def album_assets(self, album) -> list[RemoteAsset]:
        """Fotos de un álbum (con URL recién hechas). Los álbumes son pequeños comparados con la
        fototeca: se leen enteros."""
        # Paginado propio, ascendente (el del listado completo, que cuadra) y con tope de páginas: el de
        # pyicloud va hacia atrás y, con el índice de fotos, no era fiable (2026-10-25)
        count = int(self._call(lambda: len(album)) or 0)
        page_size = album.page_size
        # Apple devuelve páginas que se solapan: se sigue mientras lleguen fotos nuevas (tope amplio por si
        # acaso). Con el tope corto de antes, Oculto se quedaba en 27.287 de 27.681 (2026-10-26)
        max_pages = count // page_size * 4 + 10
        out, seen, offset = [], set(), 0
        for _ in range(max_pages):
            page = self._call(lambda: list(album._get_photos_at(offset, DirectionEnum.ASCENDING, page_size)))
            now = time.time()
            new = 0
            for asset in page:
                if asset.id in seen:
                    continue
                seen.add(asset.id)
                new += 1
                self._handles[asset.id] = asset
                ra = describe(asset)
                for rf in ra.files.values():
                    rf.listed_at = now
                out.append(ra)
            if self.budget:
                self.budget.page()
            if len(page) < page_size // 2 or not new or (count and len(out) >= count):
                break
            offset += len(page)
        else:
            log.warning("Fotos: el álbum %s no termina tras %d páginas; se usa lo leído", album.name, max_pages)
        log.info("Fotos: álbum %s: %d fotos (Apple dice %d)", album.name, len(out), count)
        return out

    def _changes_raw(self, lib, since: str) -> tuple[set[str], set[str]]:
        """Lista de cambios sin validar con pydantic: de cada registro solo importan el nombre, si está borrado y su
        tipo. Lo que no tiene sentido (sin tipo y sin estar borrado) se cuenta y se ignora."""
        changed, deleted, ignored = set(), set(), []
        self.albums_changed = False
        # El cliente de Fotos envuelve al genérico (CloudKitContainerClient), que es quien tiene raw_post
        raw_post = getattr(lib._client, "raw_post", None) or lib._client._client.raw_post
        zone_req = CKZoneChangesZoneReq(zoneID=CKZoneID(**lib.zone_id), syncToken=since, reverse=False)
        for _ in range(1000):                                  # tope de páginas por si Apple no acaba nunca
            payload = CKZoneChangesRequest(zones=[zone_req]).model_dump(mode="json", exclude_none=True)
            data = raw_post("/changes/zone", payload)
            zone = (data.get("zones") or [None])[0]
            if not zone:
                break
            if zone.get("serverErrorCode"):
                raise IOError(f"Apple da error en la lista de cambios: {zone.get('serverErrorCode')}")
            for rec in zone.get("records") or []:
                name, rtype = rec.get("recordName"), rec.get("recordType")
                if not name:
                    ignored.append("?")
                elif rec.get("deleted") is True:
                    deleted.add(name)
                elif rtype == "CPLAsset":
                    changed.add(name)
                elif rtype in ("CPLContainerRelation", "CPLAlbum"):
                    self.albums_changed = True
                elif not rtype:
                    ignored.append(name)
            lib._current_sync_token = zone.get("syncToken") or lib._current_sync_token
            if not zone.get("moreComing"):
                break
            zone_req.syncToken = zone.get("syncToken")
        if ignored:
            log.warning("Fotos: %d registros de la lista de cambios sin tipo, ignorados (p. ej. %s)", len(ignored),
                        ignored[0])
        return changed, deleted

    def changes(self, since: str) -> tuple[set[str], set[str], str | None]:
        """Lista de cambios de Apple desde el token `since`: (ids de CPLAsset cambiados, ids borrados del
        todo, token nuevo). Si no ha cambiado nada, son muy pocas peticiones."""
        lib = self._library()

        def read():
            changed, deleted = set(), set()
            self.albums_changed = False
            try:
                for ev in lib.iter_changes(since=since):
                    if ev.kind == "deleted":
                        deleted.add(ev.record_name)
                    elif ev.record_type == "CPLAsset":
                        changed.add(ev.record_name)
                    elif ev.record_type in ("CPLContainerRelation", "CPLAlbum"):
                        self.albums_changed = True     # una foto entró/salió de un álbum, o álbum nuevo
            except CloudKitApiError as exc:
                if "validation failed" not in str(exc):
                    raise
                # 6/11: Apple devolvió un registro de forma rara (sin tipo ni fecha de creación) y pyicloud rechaza
                # la página entera. Se relee la misma lista sin validar y se ignora lo que no se entiende.
                log.warning("Fotos: pyicloud no valida la lista de cambios de Apple; se lee sin validar (%s)",
                            str(exc.__cause__ or exc).splitlines()[0][:120])
                changed, deleted = self._changes_raw(lib, since)
            return changed, deleted, lib._current_sync_token
        return self._call(read)

    # --- transferencias ---------------------------------------------------------------------------

    def download(self, rf: RemoteFile, dest: Path, mtime: float, asset_id: str = "") -> int:
        """Descarga por trozos a un temporal junto al destino y lo renombra al final."""
        if not rf.url:
            raise IOError("Apple no da URL de descarga para este fichero")
        api = self.auth.require_api()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")

        def open_asset():
            if asset_id and rf.listed_at and time.time() - rf.listed_at > URL_MAX_AGE_S:
                self.refresh_urls([(asset_id, rf)])      # el motor suele renovarlas antes, en bloque
            try:
                return self._get_asset(api, rf)
            except PyiCloudAPIResponseException as exc:
                if _status(exc) != 410 or not asset_id:
                    raise
            # 410 con las dos variantes: la URL puede haber caducado (el listado de la fototeca entera
            # tarda). Se pide otra vez el registro de esta foto y se prueba con su URL nueva.
            if not self.refresh_urls([(asset_id, rf)]):
                raise IOError("Apple da 410 y la foto ya no aparece al pedirla de nuevo")
            return self._get_asset(api, rf)

        def fetch() -> int:
            resp = open_asset()
            try:
                written = 0
                deadline = time.time() + max_download_seconds(rf.size, self.max_file_s)
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        if time.time() > deadline:
                            raise SlowDownload(f"descarga demasiado lenta: {written} de {rf.size} bytes "
                                               f"en {int(max_download_seconds(rf.size, self.max_file_s))} s")
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            if self.progress:
                                self.progress.add_bytes(len(chunk))
                return written
            finally:
                resp.close()

        try:
            written = self._call(fetch)
            if rf.size and written != rf.size:
                raise IOError(f"descarga incompleta: {written} de {rf.size} bytes")
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dest)
            return written
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def url_variants(rf: RemoteFile) -> list[str]:
        """Primero la URL tal cual la da Apple (como pyicloud e icloudpd, con su "${f}" dentro); después,
        con el nombre del fichero en el sitio de "${f}", como hace icloud.com."""
        urls = [rf.url]
        if "${f}" in rf.url:
            urls.append(rf.url.replace("${f}", quote(rf.name)))
        return urls

    def _get_asset(self, api, rf: RemoteFile, headers: dict | None = None):
        urls = self.url_variants(rf)
        for i, url in enumerate(urls):
            try:
                resp = api.session.get(url, stream=True, timeout=(30, 120), headers=headers or {})
            except PyiCloudAPIResponseException as exc:
                if _status(exc) == 410 and i < len(urls) - 1:
                    continue                          # 410 (Gone): se prueba la siguiente variante
                raise
            code = getattr(resp, "status_code", 200)
            if code >= 400:
                resp.close()
                if code == 410 and i < len(urls) - 1:
                    continue
                raise PyiCloudAPIResponseException(f"HTTP {code} al descargar", code)
            return resp
        raise IOError("sin URL de descarga")

    def upload(self, path: Path) -> tuple[str, bool]:
        """Sube un fichero a la fototeca. Devuelve (id del asset, Apple dice que ya estaba)."""
        lib = self._library()
        zone = str(lib.zone_id.get("zoneName") or "")
        result = self._call(lambda: lib._client.upload_file(str(path), zone_name=zone))
        if self.progress:
            self.progress.add_bytes(path.stat().st_size, upload=True)
        if not result.cplAsset:
            raise IOError("Apple no devolvió el id de la foto subida")
        return result.cplAsset, bool(result.is_duplicate)

    def delete(self, asset_id: str) -> None:
        """Marca la foto como borrada: va a Eliminados recientemente (30 días)."""
        handle = self._handles.get(asset_id)
        if handle is None:
            raise IOError("la foto no está en el último listado de iCloud")
        if not self._call(handle.delete):
            raise IOError("Apple no confirmó el borrado")
