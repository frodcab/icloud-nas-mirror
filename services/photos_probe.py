# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Sonda de iCloud Fotos (fase 0 de la sync de la fototeca), sobre la MISMA sesión de Apple que Drive.

Dos partes:
- Lectura (una vez por arranque, sin cambiar nada): bibliotecas, nº de fotos, álbumes, cursor de
  cambios y una muestra de fotos recientes (versiones, Live Photo, ediciones del iPhone).
- Escritura (solo si existe data/PRUEBA_FOTOS): sube una imagen sintética fechada en 2001, la sube
  otra vez (Apple debe decir "duplicada") y la borra (va a "Eliminados recientemente"). Al acabar,
  el fichero pasa a PRUEBA_FOTOS.hecho con el resultado.

Resultado en el log ("Sonda Fotos") y en state/photos_probe.json. Depende de internos de
pyicloud 2.7.0 (versión fijada en requirements.txt).
"""

import json
import logging
import os
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pyicloud.services.photos_cloudkit.mappers import record_field_value

log = logging.getLogger(__name__)

SAMPLE = 300             # fotos recientes que se miran (unas pocas páginas de la API)
CHANGES_SAMPLE = 50      # eventos de la lista de cambios que se leen
# Campos de la edición hecha en el iPhone: pyicloud los pide pero no los ofrece como versión
EDIT_FIELDS = ("adjustmentType", "adjustmentRenderType", "resJPEGFullRes", "resJPEGFullFileType",
               "resVidFullRes", "resVidFullFileType")


def _value(record, field):
    try:
        return record_field_value(record, field)
    except Exception:
        return None


def _short(value) -> str:
    """Resumen de un valor de CloudKit sin URLs firmadas (no deben quedar en el log)."""
    if value is None:
        return "-"
    size = getattr(value, "size", None)
    if size is None and isinstance(value, dict):
        size = value.get("size")
    if size is not None:
        return f"asset {size / 2**20:.1f} MB"
    return str(value)[:60]


class PhotosProbe:
    def __init__(self, auth, state_dir: Path, trigger_file: Path):
        self.auth = auth
        self.out = state_dir / "photos_probe.json"
        self.trigger_file = trigger_file
        self.read_done = False

    def run(self) -> None:
        """Nunca lanza: un fallo aquí no puede parar la sync de Drive."""
        if self.read_done and not self.trigger_file.exists():
            return
        try:
            api = self.auth.require_api()
            photos = api.photos
        except Exception as exc:
            log.warning("Photos probe: cannot open iCloud Photos (%s: %s)", type(exc).__name__, exc)
            self.read_done = True
            return
        report: dict = {"fecha": datetime.now(timezone.utc).isoformat()}
        if not self.read_done:
            self.read_done = True
            try:
                report["lectura"] = self._read(photos)
            except Exception as exc:
                log.exception("Photos probe: the read failed")
                report["lectura"] = {"error": f"{type(exc).__name__}: {exc}"}
        if self.trigger_file.exists():
            try:
                report["escritura"] = self._write(photos)
            except Exception as exc:
                log.exception("Photos probe: the write test failed")
                report["escritura"] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                self.trigger_file.with_name(self.trigger_file.name + ".hecho").write_text(
                    json.dumps(report.get("escritura"), ensure_ascii=False, indent=2), encoding="utf-8")
                self.trigger_file.unlink()
            except OSError:
                log.warning("Photos probe: could not remove %s; it would be retried in the next run", self.trigger_file)
        try:
            previous = json.loads(self.out.read_text(encoding="utf-8")) if self.out.exists() else {}
            previous.update(report)
            self.out.write_text(json.dumps(previous, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except (OSError, ValueError):
            log.warning("Photos probe: could not save %s", self.out)

    # --- lectura ---------------------------------------------------------------------------------

    def _read(self, photos) -> dict:
        r: dict = {}
        t0 = time.time()
        r["bibliotecas"] = sorted(photos.libraries.keys())
        library = photos.libraries.get("root") or photos.libraries[r["bibliotecas"][0]]
        r["zona"] = getattr(library, "zone_id", {}).get("zoneName")
        r["subida_disponible"] = getattr(getattr(library, "_client", None), "_uploader", None) is not None
        everything = library.all
        r["total_fotos"] = len(everything)
        try:
            r["albumes"] = {a.name: len(a) for a in library.albums}
        except Exception as exc:
            r["albumes"] = f"error {type(exc).__name__}: {exc}"
        try:
            r["cursor"] = bool(library.sync_cursor())
            kinds: dict[str, int] = {}
            for i, ev in enumerate(library.iter_changes(since=None)):
                key = f"{ev.kind}:{ev.record_type or '-'}"
                kinds[key] = kinds.get(key, 0) + 1
                if i + 1 >= CHANGES_SAMPLE:
                    break
            r["cambios_muestra"] = kinds
        except Exception as exc:
            r["cambios_muestra"] = f"error {type(exc).__name__}: {exc}"

        stats = {"fotos": 0, "videos": 0, "live": 0, "editadas": 0, "con_alternativa": 0,
                 "edicion_en_asset": 0, "edicion_en_master": 0, "tipos": {}, "versiones": {}}
        examples: list[dict] = []
        first = None
        for i, asset in enumerate(everything.photos):
            if i >= SAMPLE:
                break
            first = first or asset
            master, rec = asset._master_record, asset._asset_record
            kind = asset.item_type
            stats["videos" if kind == "movie" else "fotos"] += 1
            stats["live"] += asset.is_live_photo
            ext = os.path.splitext(asset.filename)[1].lower()
            stats["tipos"][ext] = stats["tipos"].get(ext, 0) + 1
            for v in asset.resources:
                stats["versiones"][v] = stats["versiones"].get(v, 0) + 1
            stats["con_alternativa"] += "alternative" in asset.resources
            edit_asset = {f: _value(rec, f) for f in EDIT_FIELDS}
            edit_master = {f: _value(master, f) for f in EDIT_FIELDS}
            edited = any(v is not None for v in edit_asset.values()) or any(
                v is not None for v in edit_master.values())
            stats["editadas"] += edited
            stats["edicion_en_asset"] += any(v is not None for v in edit_asset.values())
            stats["edicion_en_master"] += any(v is not None for v in edit_master.values())
            if len(examples) < 8 and (edited or asset.is_live_photo or kind == "movie" or i < 2):
                examples.append({
                    "nombre": asset.filename, "tipo": kind, "live": asset.is_live_photo,
                    "fecha": asset.asset_date.isoformat(), "tamaño_MB": round((asset.size or 0) / 2**20, 1),
                    "versiones": {k: f"{(res.size or 0) / 2**20:.1f} MB {res.type}"
                                  for k, res in asset.resources.items()},
                    "edicion_asset": {k: _short(v) for k, v in edit_asset.items() if v is not None},
                    "edicion_master": {k: _short(v) for k, v in edit_master.items() if v is not None},
                })
        r["muestra"] = stats
        r["ejemplos"] = examples
        try:
            r["prueba_url"] = self._url_test(photos, first)
        except Exception as exc:
            r["prueba_url"] = {"error": f"{type(exc).__name__}: {exc}"}
        r["segundos"] = round(time.time() - t0, 1)
        log.info("Sonda Fotos (lectura): %s", json.dumps(r, ensure_ascii=False, default=str))
        return r

    def _url_test(self, photos, asset) -> dict:
        """¿Qué URL de descarga acepta Apple? Pide UN byte (Range 0-0) de una foto con cada variante y
        apunta solo el código HTTP (nada del contenido)."""
        from services.photos_remote import ICloudPhotosRemote, _status, describe
        if asset is None:
            return {"error": "sin fotos"}
        rf = describe(asset).files.get("original")
        if rf is None or not rf.url:
            return {"error": "sin URL"}
        out = {"tiene_${f}": "${f}" in rf.url, "host": rf.url.split("/")[2] if "//" in rf.url else "?"}
        api = self.auth.require_api()
        for name, url in zip(("tal_cual", "con_nombre"), ICloudPhotosRemote.url_variants(rf)):
            try:
                resp = api.session.get(url, stream=True, timeout=30, headers={"Range": "bytes=0-0"})
                out[name] = resp.status_code
                resp.close()
            except Exception as exc:
                out[name] = f"{type(exc).__name__} {_status(exc)}"
        log.info("Photos probe (download URL): %s", out)
        return out

    # --- escritura (solo con data/PRUEBA_FOTOS) ---------------------------------------------------

    def _write(self, photos) -> dict:
        from PIL import Image

        library = photos.libraries["root"]
        client = library._client
        r: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"prueba-icloud-sync-{int(time.time())}.jpg"
            # Ruido aleatorio: no puede coincidir con ninguna foto real (Apple deduplica por contenido)
            img = Image.frombytes("RGB", (64, 64), random.randbytes(64 * 64 * 3))
            exif = Image.Exif()
            exif[0x0132] = "2001:01:01 12:00:00"                         # DateTime
            exif.get_ifd(0x8769)[0x9003] = "2001:01:01 12:00:00"         # DateTimeOriginal
            img.save(path, "JPEG", quality=90, exif=exif)
            old = datetime(2001, 1, 1, 12, tzinfo=timezone.utc).timestamp()
            os.utime(path, (old, old))

            t0 = time.time()
            first = client.upload_file(str(path), zone_name=str(library.zone_id.get("zoneName") or ""))
            r["subida"] = {"duplicada": first.is_duplicate, "asset": first.cplAsset,
                           "segundos": round(time.time() - t0, 1)}
            second = client.upload_file(str(path), zone_name=str(library.zone_id.get("zoneName") or ""))
            r["resubida"] = {"duplicada": second.is_duplicate, "mismo_asset": second.cplAsset == first.cplAsset}

        t0 = time.time()
        asset = library._hydrate_uploaded_asset(first)
        r["indexada"] = asset is not None
        r["segundos_hasta_indexar"] = round(time.time() - t0, 1)
        if asset is None:
            r["borrado"] = "no indexada a tiempo: queda en la fototeca, bórrala a mano (fecha 1/1/2001)"
        else:
            r["fecha_en_icloud"] = asset.asset_date.isoformat()
            r["borrado"] = bool(asset.delete())
        log.info("Sonda Fotos (escritura): %s", json.dumps(r, ensure_ascii=False, default=str))
        return r
