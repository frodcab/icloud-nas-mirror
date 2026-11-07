# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Sync bidireccional de iCloud Fotos con una carpeta del NAS (AAAA/MM/), dentro de los límites del run.

Reglas (README, "iCloud Fotos"):
- Foto nueva en iCloud → se descarga a AAAA/MM/ (fecha de la foto, hora local): el original,
  el vídeo del Live Photo y, si se editó en el iPhone, la edición como "<nombre> (editada).<ext>".
- Fichero nuevo en Subir/ → se sube a Fotos y, cuando aparece en el listado, se mueve a su AAAA/MM/.
- Original borrado en el NAS → se borra en iCloud (Eliminados recientemente, 30 días).
- Foto borrada en iCloud → sus ficheros van a la papelera del NAS.
  Más de CONFIRM_DELETES_OVER borrados (de los dos lados) → esperan confirmación en el panel.
- Borrar en el NAS solo el vídeo del Live Photo o la edición → no se vuelve a bajar; iCloud no se toca.
- Edición nueva en el iPhone → se baja otra vez la edición (la anterior va a la papelera).
- Editar un fichero en el NAS NO se sube (Fotos no deja sustituir un original): se avisa una vez.
- Mover o renombrar en el NAS → solo cambia en el NAS (se sigue el fichero por nombre y tamaño).
Salvaguardas: si el listado de iCloud sale incompleto no se borra nada en el NAS; si la fototeca o
la carpeta aparecen vacías (o casi), se aborta; nada se borra sin pasar por una papelera.
"""

import json
import logging
import os
import random
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from services.entries import AuthRequired, RunResult, SyncAborted
from services.local_fs import LocalTree, is_excluded
from services.photos_db import PhotoFile, PhotosDB
from services.photos_limits import PhotosLimits, RunBudget
from services.photos_albums import ALBUMS_DIR, EXTERNAL_HIDDEN, HIDDEN_DIR, AlbumsMirror
from services.photos_remote import GONE, HIDDEN, LOOKUP_BATCH, URL_MAX_AGE_S, RemoteAsset, RemoteFile, Throttled
from services.progress import Progress

log = logging.getLogger(__name__)

UPLOADED = ".subidos"            # dentro de Subir/: ya subidas, esperando a que iCloud las indexe
REJECTED = "_no_admitidas"       # dentro de Subir/: Apple las rechazó 3 veces
UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".dng", ".cr2", ".cr3",
                     ".nef", ".arw", ".raf", ".orf", ".rw2", ".mov", ".mp4", ".m4v", ".3gp"}
SUFFIX = {"original": "", "live": "", "edited": " (editada)", "edited_live": " (editada)"}
LISTING_COMPLETE = 0.98          # listado < 98 % de lo que dice el contador → no se borra nada en el NAS
FULL_EVERY_S = 7 * 86400         # vuelta completa (red de seguridad) una vez por semana
BACKFILL_END_IDLE_PAGES = 5      # páginas seguidas sin nada nuevo, ya pasado lo cubierto: es el final del índice
BACKFILL_OVERLAP = 200           # al saltar a donde se quedó la carga, se repasan estas por si se movieron
RETRY_BASE_S = 1800              # un fichero que falla espera 30 min, 1 h, 2 h... (tope 24 h) antes de reintentarse
RETRY_MAX_S = 86400
RETRY_FULL_S = 86400             # con ficheros fallidos pendientes, como mucho una vuelta completa al día
MAX_CONSECUTIVE_ERRORS = 10      # fallos seguidos de descarga → se corta la vuelta (no martillear a Apple)


@dataclass
class DeleteCandidate:
    kind: str          # photo_icloud (borrada en el NAS → borrar en iCloud) | photo_nas (al revés)
    path: str          # ruta del original en el NAS (lo que se enseña en el panel)
    size: int
    side: str          # icloud | nas: dónde se borraría
    asset_id: str


class PhotosEngine:
    def __init__(self, db: PhotosDB, root: Path, trash_dir: Path, remote, limits: PhotosLimits,
                 excludes: list[str], confirm_deletes_over: int, progress: Progress | None = None,
                 upload_dir: str = "Subir", min_age_s: float = 120, sleep: Callable[[float], None] = time.sleep,
                 hidden_enabled: bool = True, hidden_dir: Path | None = None,
                 random_pages: int = 0, random_share: float = 0.3):
        self.db, self.root, self.remote, self.limits = db, root, remote, limits
        self.local = LocalTree(root, trash_dir, trash_prefix="Fotos")
        self.excludes = excludes
        self.confirm_over = confirm_deletes_over
        self.progress = progress or Progress()
        self.upload_dir = upload_dir
        self.min_age_s = min_age_s
        self.sleep = sleep
        self.backfill_overlap = BACKFILL_OVERLAP
        self.random_pages = random_pages      # páginas al azar por vuelta durante la carga inicial (0 = ninguna)
        self.random_share = random_share      # parte del cupo de descargas que pueden gastar
        # Ocultas: dentro (Álbumes/Oculto) o en otra carpeta (PHOTOS_HIDDEN_DIR); fuera de la carpeta de
        # Fotos se guardan en photos.db como "@oculto/<nombre>" y abs() las resuelve
        self.hidden_enabled = hidden_enabled
        self.hidden_dir = Path(hidden_dir) if hidden_dir else None
        if self.hidden_dir is None:
            self.hidden_prefix = HIDDEN_DIR
        else:
            try:
                self.hidden_prefix = str(self.hidden_dir.relative_to(self.root))
            except ValueError:
                self.hidden_prefix = EXTERNAL_HIDDEN
        self.albums = AlbumsMirror(self)

    # --- rutas ----------------------------------------------------------------------------------

    def abs(self, path: str) -> Path:
        """Ruta real de un fichero de photos.db (relativa a la carpeta de Fotos, o "@oculto/…")."""
        if path == EXTERNAL_HIDDEN or path.startswith(EXTERNAL_HIDDEN + "/"):
            return self.hidden_dir / path[len(EXTERNAL_HIDDEN):].lstrip("/")
        return self.root / path

    def is_hidden_path(self, path: str) -> bool:
        return path.startswith(self.hidden_prefix + "/")

    def _trash(self, path: str) -> None:
        dest = self.local.trash_dir / datetime.now().strftime("%Y-%m-%d") / "Fotos" / path.replace(EXTERNAL_HIDDEN, "Oculto", 1)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.name}.{int(time.time())}")
        shutil.move(str(self.abs(path)), str(dest))

    # --- run ------------------------------------------------------------------------------------

    def clean_orphan_parts(self, max_age_s: float = 3600) -> int:
        """Borra los temporales de descarga (.nombre.part) que dejó un corte o un reinicio. Una vez por arranque."""
        n, now = 0, time.time()
        for base in {self.root, self.abs(self.hidden_prefix + "/x").parent}:
            if not base.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(base):
                if Path(dirpath).name == ALBUMS_DIR:
                    continue
                for name in files:
                    if name.startswith(".") and name.endswith(".part"):
                        f = Path(dirpath) / name
                        try:
                            if now - f.stat().st_mtime > max_age_s:
                                f.unlink()
                                n += 1
                        except OSError:
                            pass
        if n:
            log.info("Fotos: %d temporales de descargas cortadas borrados", n)
        return n

    def run(self, dry_run: bool = False) -> tuple[RunResult, RunBudget]:
        if not getattr(self, "_parts_cleaned", False):
            self._parts_cleaned = True
            if not dry_run:
                try:
                    self.clean_orphan_parts()
                except Exception:
                    log.exception("Fotos: no se pudieron limpiar los temporales")
        budget = RunBudget(self.limits, sleep=self.sleep)
        self.remote.budget = budget
        result = RunResult(dry_run=dry_run)
        self._result, self._dry = result, dry_run
        self._unfinished = 0      # operaciones que Apple frenó: el run no cuenta como completo
        self._streak = 0          # descargas fallidas seguidas en esta vuelta (cortacircuitos)
        if not self.root.is_dir():
            raise SyncAborted(f"No existe la carpeta de Fotos {self.root} (¿volumen sin montar?)")

        self.progress.set_phase("scanning")
        known = self.db.files()
        local = self._scan()
        if known and not local and len(known) > 20:
            raise SyncAborted("La carpeta de Fotos del NAS aparece vacía pero hay fotos sincronizadas: "
                              "¿volumen sin montar? No se toca nada")
        inbox = self._scan_inbox()
        decisions = self.db.decisions()

        uploaded_now = self._uploads(inbox, known, budget) if inbox else 0
        uploads = self.db.uploads()

        if self.hidden_prefix != HIDDEN_DIR and not dry_run:
            moved = self.albums.migrate_hidden(HIDDEN_DIR, known)
            if moved:
                known, local = self.db.files(), self._scan()
        self._hidden = self.albums.hidden_ids
        if self.albums.due() and not budget.exhausted:
            try:
                self.albums.refresh(local, uploads, budget)
            except (AuthRequired, SyncAborted):
                raise
            except Exception as exc:
                log.exception("Fotos: no se pudieron revisar los álbumes")
                result.warnings.append(f"No se pudieron revisar los álbumes: {type(exc).__name__}: {exc}")
            self._hidden = self.albums.hidden_ids
            known, local = self.db.files(), self._scan()     # Oculto puede haber movido ficheros

        if self.hidden_enabled and not dry_run and not budget.exhausted and self._hidden:
            try:
                self._hidden_downloads(local, uploads, budget)
            except (AuthRequired, SyncAborted):
                raise
            except Exception as exc:
                log.exception("Fotos: fallo bajando las ocultas")
                result.warnings.append(f"Ocultas: {type(exc).__name__}: {exc}")
            known, local = self.db.files(), self._scan()

        if self.random_pages > 0 and not dry_run and not budget.exhausted and self.db.get("backfill_done") != "1":
            try:
                self._random_pages(local, uploads, budget)
            except (AuthRequired, SyncAborted):
                raise
            except Throttled as exc:
                budget.throttled(str(exc))
            except Exception as exc:
                log.exception("Fotos: fallo en las páginas al azar")
                result.warnings.append(f"Páginas al azar: {type(exc).__name__}: {exc}")
            known, local = self.db.files(), self._scan()     # lo bajado aquí debe verse como presente en el NAS

        mode = self.mode()
        self._result.mode = mode
        if mode != "full":
            out = self._run_incremental(mode, known, local, uploads, decisions, budget)
        else:
            out = self._run_full(known, local, uploads, decisions, budget, uploaded_now)
        try:
            self.albums.relink()
        except Exception as exc:
            log.exception("Fotos: no se pudieron actualizar las carpetas de álbumes")
            result.warnings.append(f"Carpetas de álbumes: {type(exc).__name__}: {exc}")
        return out

    def _random_pages(self, local, uploads, budget: RunBudget) -> None:
        """Sondas en páginas al azar, también de números altos, durante la carga inicial: si el recorrido en orden
        se atasca o va lento, cada vuelta sigue bajando fotos de otros sitios y, por repetición, se llega a
        todas. Cada sonda deja en el log cuánto tardó la página (y en photos.db las últimas 10)."""
        upper = max(int(self.db.get("backfill_k") or 0), self.remote.count())
        end = int(self.db.get("random_end") or 0)
        if end:
            upper = min(upper, end)           # ya se vio que el índice acaba ahí
        cap = max(1, int(self.limits.max_downloads * self.random_share))
        probes = json.loads(self.db.get("random_probe") or "[]")
        planned = tries = 0
        self.progress.set_phase("transferring")
        done = 0
        while done < self.random_pages and tries < self.random_pages * 2 and planned < cap and upper > 0:
            if budget.exhausted or self.progress.stop_requested or self._streak >= MAX_CONSECUTIVE_ERRORS:
                break
            tries += 1
            pos = random.randrange(upper)
            page, n, page_size, secs = self.remote.page_at(pos)
            probes.append({"pos": pos, "items": n, "visible": len(page), "s": round(secs, 1),
                           "at": datetime.now().isoformat(timespec="seconds")})
            log.info("Fotos: sonda en la página alta %d: %d elementos (%d visibles) en %.1f s", pos, n, len(page), secs)
            if n == 0:
                upper = pos                    # más allá no hay nada: se acorta el rango y se sigue
                self.db.set("random_end", str(upper))
                continue
            done += 1
            downloads, _ = self._plan({ra.id: ra for ra in page}, self.db.files(), local, uploads, complete=False)
            downloads.sort(key=lambda d: d[0].date, reverse=True)
            downloads = downloads[:cap - planned]
            planned += len(downloads)
            if downloads:
                self.progress.set_plan(self.progress.actions_total + len(downloads),
                                       self.progress.bytes_total + sum(d[1].size for d in downloads))
                self._run_downloads(downloads, budget)
        self.db.set("random_probe", json.dumps(probes[-10:]))
        if planned:
            log.info("Fotos: %d ficheros bajados de páginas al azar", planned)

    def _hidden_downloads(self, local, uploads, budget: RunBudget) -> None:
        """Ocultas que faltan en el NAS: se piden por id (URL recién hechas, también para las ocultas) en
        bloques y se bajan a su carpeta, con la mitad del cupo de la vuelta como mucho (la otra mitad
        sigue con las visibles)."""
        known_ids = {f.asset_id for f in self.db.files().values()}
        pending = sorted(self._hidden - known_ids)
        if not pending:
            return
        cap = max(1, self.limits.max_downloads // 2)
        log.info("Fotos: %d ocultas por bajar; en esta vuelta, hasta %d ficheros", len(pending), cap)
        planned = 0
        self.progress.set_phase("transferring")
        for i in range(0, len(pending), LOOKUP_BATCH):
            if planned >= cap or budget.exhausted or self.progress.stop_requested or self._streak >= MAX_CONSECUTIVE_ERRORS:
                break
            chunk = pending[i:i + LOOKUP_BATCH]
            looked = self.remote.lookup(chunk)
            hidden = dict(getattr(self.remote, "hidden_assets", {}))
            for aid, v in looked.items():
                if v == GONE or isinstance(v, RemoteAsset):
                    self._hidden.discard(aid)      # borrada, o ya no está oculta (la carga normal la baja)
            downloads, _ = self._plan(hidden, self.db.files(), local, uploads, complete=False,
                                      folders={aid: self.hidden_prefix for aid in hidden})
            downloads = downloads[:cap - planned]
            planned += len(downloads)
            self.progress.set_plan(self.progress.actions_total + len(downloads),
                                   self.progress.bytes_total + sum(d[1].size for d in downloads))
            self._run_downloads(downloads, budget)
        self.albums._save_hidden(self._hidden)

    def mode(self) -> str:
        """backfill: carga inicial por bloques (de lo más reciente a lo más antiguo).
        changes: ya está todo; solo la lista de cambios de Apple. full: listado entero (una vez por
        semana, red de seguridad por si la lista de cambios se dejó algo)."""
        if self.db.get("backfill_done") != "1":
            return "backfill"
        if not self.db.get("changes_token"):
            return "full"
        since_full = time.time() - float(self.db.get("last_full") or 0)
        if since_full > FULL_EVERY_S:
            return "full"
        if since_full > RETRY_FULL_S and self._retry_due():
            return "full"          # hay ficheros que fallaron y ya les toca: solo el listado entero los vuelve a ver
        return "changes"

    def _run_full(self, known, local, uploads, decisions, budget: RunBudget, uploaded_now: int):
        result, dry_run = self._result, self._dry
        cursor = self.remote.cursor()
        missing_originals = any(f.role == "original" and not f.dismissed and f.path not in local
                                for f in known.values())
        if (cursor and cursor == self.db.get("cursor") and self.db.get("complete") == "1"
                and not missing_originals and not uploads and not decisions and not uploaded_now):
            self._local_only(known, local)
            result.message = result.message or "Sin cambios en iCloud"
            return self._finish(result, budget, cursor, complete=True)

        self.progress.set_phase("listing")
        expected = self.remote.count()
        assets: dict[str, RemoteAsset] = {}
        # Solo de las que se pueden borrar en iCloud (original borrado en el NAS) hace falta el objeto de Apple
        keep = {f.asset_id for f in known.values() if f.role == "original" and not f.dismissed and f.path not in local}
        for ra in self.remote.list_assets(keep=keep):
            assets[ra.id] = ra
            if self.progress.stop_requested:
                result.status, result.message = "stopped", "Parada a petición del usuario durante el listado"
                return self._finish(result, budget, cursor, complete=False)
        complete = len(assets) >= expected * LISTING_COMPLETE
        known_assets = {f.asset_id for f in known.values()}
        if known_assets and len(known_assets) > 20 and len(assets) < 0.5 * len(known_assets):
            raise SyncAborted(f"iCloud devuelve {len(assets)} fotos y en el NAS hay {len(known_assets)} "
                              "sincronizadas: respuesta rota de Apple, no se toca nada")
        if not complete:
            result.warnings.append(f"El listado de iCloud ha salido incompleto ({len(assets)} de {expected}): "
                                   "en este run no se borra nada en el NAS")

        self.progress.set_phase("planning")
        downloads, deletes = self._plan(assets, known, local, uploads, complete)
        execute, restore = self._decide_deletes(deletes, decisions)
        for c in restore:
            downloads.extend(self._restore(c, assets, known))

        self.progress.set_phase("transferring")
        downloads.sort(key=lambda d: d[0].date, reverse=True)       # lo más reciente primero
        self.progress.set_plan(len(downloads) + len(execute), sum(d[1].size for d in downloads))
        if dry_run:
            self._log_plan(downloads, execute)
        else:
            self._run_deletes(execute, known, budget)
            self._run_downloads(downloads, budget)
            if decisions:
                self.db.clear_decisions(decisions)
        deferred = sum(budget.deferred.values())
        if deferred and not result.message:
            result.message = f"Límite de la vuelta alcanzado: {budget.summary()}"
        done = (complete and not deferred and not result.errors and not self._unfinished and not budget.exhausted
                and not result.pending_deletes and result.status == "ok")
        if done and not dry_run and cursor:
            # Todo cuadra: las próximas vueltas solo miran la lista de cambios desde aquí
            self.db.set("changes_token", cursor)
            self.db.set("last_full", str(time.time()))
            self.db.set("backfill_done", "1")
        return self._finish(result, budget, cursor, complete=done)

    def _run_incremental(self, mode: str, known, local, uploads, decisions, budget: RunBudget):
        """Sin listar la fototeca entera: lo borrado en el NAS y lo que Apple dice que ha cambiado se
        piden por id, en bloque; en la carga inicial, además, se recorren páginas de la más reciente a la
        más antigua bajando cada una al momento (URL recién hechas)."""
        result, dry_run = self._result, self._dry
        known_assets = {f.asset_id for f in known.values()}
        gone = set(json.loads(self.db.get("gone_ids") or "[]")) & known_assets
        to_lookup = {f.asset_id for f in known.values()
                     if f.role == "original" and not f.dismissed and f.path not in local}
        new_token = None
        self.progress.set_phase("listing")
        if mode == "changes":
            token = self.db.get("changes_token")
            changed, deleted, new_token = self.remote.changes(token)
            gone |= deleted & known_assets
            to_lookup |= changed | gone
            log.info("Fotos: la lista de cambios de Apple trae %d fotos cambiadas y %d borradas", len(changed),
                     len(deleted))
        else:
            to_lookup |= gone
        to_lookup |= set(uploads)              # subidas desde Subir/ esperando a colocarse
        if mode == "changes" and getattr(self.remote, "albums_changed", False) and not dry_run:
            self.db.set("albums_dirty", "1")    # la próxima vuelta repasa los álbumes
        looked = self.remote.lookup(sorted(to_lookup)) if to_lookup else {}
        alive = {aid: ra for aid, ra in looked.items() if not isinstance(ra, str)}
        hidden_now = {aid for aid, ra in looked.items() if ra == HIDDEN and aid in known_assets}
        for aid, ra in looked.items():
            if ra == HIDDEN:
                gone.discard(aid)
            elif isinstance(ra, str) and aid in known_assets:
                gone.add(aid)
            elif aid in alive:
                gone.discard(aid)              # p.ej. recuperada de Eliminados recientemente
        # Ocultada / mostrada en iCloud: a Álbumes/Oculto o de vuelta a su mes
        moved = False
        if self.hidden_enabled:
            for aid in hidden_now - self._hidden:
                self.albums.hide(aid, known)
                moved = True
        for aid in set(alive) & self._hidden:
            self.albums.unhide(aid, known)
            moved = True
        hidden_unknown = {aid for aid, ra in looked.items() if ra == HIDDEN and aid not in known_assets}
        self._hidden = (self._hidden | hidden_now | hidden_unknown) - set(alive)
        self.albums._save_hidden(self._hidden)
        if moved:
            known, local = self.db.files(), self._scan()

        self.progress.set_phase("planning")
        downloads, deletes = self._plan(alive, known, local, uploads, complete=False, gone=gone)
        for aid in hidden_now:
            # Oculta y borrada en el NAS: se borra en iCloud, como cualquier otra
            orig = next((f for f in known.values() if f.asset_id == aid and f.role == "original"), None)
            if orig and not orig.dismissed and orig.path not in local:
                deletes.append(DeleteCandidate("photo_icloud", orig.path, orig.size, "icloud", aid))
        # Lo que no se ha pedido a Apple: solo lo que se decide con el NAS (vídeo/edición borrados a mano…)
        self._local_only({p: f for p, f in known.items()
                          if f.asset_id not in alive and f.asset_id not in gone and f.asset_id not in hidden_now}, local)
        execute, restore = self._decide_deletes(deletes, decisions)
        for c in restore:
            downloads.extend(self._restore(c, alive, known))
            gone.discard(c.asset_id)
        self.progress.set_phase("transferring")
        downloads.sort(key=lambda d: d[0].date, reverse=True)
        self.progress.set_plan(len(downloads) + len(execute), sum(d[1].size for d in downloads))
        if dry_run:
            self._log_plan(downloads, execute)
        else:
            self._run_deletes(execute, known, budget)
            gone -= {c.asset_id for c in execute}
            self._run_downloads(downloads, budget)
            if decisions:
                self.db.clear_decisions(decisions)
            self.db.set("gone_ids", json.dumps(sorted(gone)) if gone else None)

        if mode == "backfill" and not self.progress.stop_requested and not budget.exhausted:
            self._backfill_pages(local, uploads, budget)
        clean = (not sum(budget.deferred.values()) and not result.errors and not self._unfinished
                 and not budget.exhausted and result.status in ("ok", "pending"))
        if mode == "changes" and clean and new_token and not dry_run:
            self.db.set("changes_token", new_token)   # si no, la próxima vuelta repite estos cambios
        if sum(budget.deferred.values()) and not result.message:
            result.message = f"Límite de la vuelta alcanzado: {budget.summary()}"
        return self._finish(result, budget, None, complete=False)

    def _backfill_pages(self, local, uploads, budget: RunBudget) -> None:
        """Carga inicial: páginas de la más reciente a la más antigua. Cada vuelta mira primero arriba
        (fotos nuevas) hasta una página ya completa y salta a donde se quedó la anterior."""
        if not self.db.get("backfill_token") and not self._dry:
            # Cambios desde AHORA: al acabar la carga, la lista de cambios trae lo borrado mientras tanto
            self.db.set("backfill_token", self.remote.cursor() or "")
        if self.db.get("backfill_scheme") != "3" and not self._dry:
            # Las posiciones de antes eran de otro índice ("Todas las fotos"): se empieza de nuevo arriba
            self.db.set("backfill_k", "0")
            self.db.set("backfill_scheme", "3")
        if hasattr(self.remote, "new_run"):
            self.remote.new_run()
        done_upto = int(self.db.get("backfill_k") or 0)   # posiciones desde arriba ya completas
        pos, jumped, pages, idle = 0, False, 0, 0
        while not self.progress.stop_requested and not budget.exhausted:
            page, advance, raw, page_size = self.remote.page_newest(pos)
            pages += 1
            if pages % 20 == 0:
                log.info("Fotos: carga inicial, %d páginas leídas en esta vuelta (posición %d, %.0f s)",
                         pages, pos, time.time() - (self.progress.started_at or time.time()))
            if not raw:
                self._backfill_finished(pos)
                break
            if not advance:
                # Apple repite fotos ya vistas en esta vuelta: se avanza una página sin bajar nada. Pasado el
                # final del índice repite la última ventana para siempre (5/11: 12 h recorriendo posiciones
                # 74.000-90.000 sin nada nuevo), así que varias páginas seguidas así, ya más allá de lo cubierto,
                # son el final.
                idle += 1
                pos += page_size
                if idle >= BACKFILL_END_IDLE_PAGES and pos >= done_upto - self.backfill_overlap:
                    log.info("Fotos: %d páginas seguidas sin nada nuevo desde la posición %d: final del índice",
                             idle, pos - idle * page_size)
                    self._backfill_finished(pos)
                    break
                continue
            idle = 0
            known = self.db.files()
            downloads, _ = self._plan({ra.id: ra for ra in page}, known, local, uploads, complete=False)
            downloads.sort(key=lambda d: d[0].date, reverse=True)
            self.progress.folder_scanned(f"Carga inicial: {pos + advance:,} fotos revisadas".replace(",", "."), advance)
            if self._dry:
                self._log_plan(downloads, [])
                if pages >= 3:
                    log.info("Fotos (simulación): carga inicial, se para tras 3 páginas")
                    break
                pos += advance
                continue
            # Los errores sueltos no frenan la carga (se reintentan en la vuelta completa semanal); el cupo
            # agotado, los frenazos de Apple o la racha de fallos, sí
            before = (sum(budget.deferred.values()), self._unfinished)
            self.progress.set_plan(self.progress.actions_total + len(downloads),
                                   self.progress.bytes_total + sum(d[1].size for d in downloads))
            self._run_downloads(downloads, budget)
            page_ok = (sum(budget.deferred.values()), self._unfinished) == before \
                and not budget.exhausted and self._result.status != "stopped" and self._streak < MAX_CONSECUTIVE_ERRORS
            start, pos = pos, pos + advance
            if not page_ok:
                break
            if start <= done_upto:
                done_upto = max(done_upto, pos)
                self.db.set("backfill_k", str(done_upto))
            if not downloads and not jumped and done_upto > pos:
                # Página ya completa: lo nuevo de arriba está visto; se salta a donde se quedó
                pos, jumped = max(pos, done_upto - self.backfill_overlap), True
            if raw < page_size // 2:
                self._backfill_finished(pos)
                break
        self.db.set("backfill_total", str(self.remote.count()))

    def _backfill_finished(self, pos: int) -> None:
        if self._dry:
            return
        log.info("Fotos: carga inicial terminada (%d fotos recorridas); a partir de ahora, solo cambios", pos)
        self.db.set("backfill_done", "1")
        self.db.set("backfill_k", str(pos))
        self.db.set("changes_token", self.db.get("backfill_token") or None)
        self.db.set("last_full", str(time.time()))

    def _finish(self, result: RunResult, budget: RunBudget, cursor, complete: bool):
        if budget.exhausted and result.status == "ok":
            result.status = "partial"
            result.message = budget.stop_reason
        elif result.errors and result.status == "ok":
            result.status = "partial"
        if not self._dry and getattr(result, "mode", "full") == "full":
            self.db.set("cursor", cursor if complete else None)
            self.db.set("complete", "1" if complete else "0")
        done = sum(result.counts.values())
        result.message = result.message or (f"{done} operaciones, {len(result.errors)} errores" if done or result.errors
                                            else "Sin cambios")
        log.info("Fotos: %s. %s", result.message, budget.summary())
        return result, budget

    # --- NAS ------------------------------------------------------------------------------------

    def _scan(self) -> dict[str, tuple[int, int]]:
        """{ruta relativa: (tamaño, mtime_ns)} de la carpeta, sin Subir/ ni temporales."""
        out: dict[str, tuple[int, int]] = {}
        # Álbumes/ son enlaces gestionados por la app (no cuentan); Álbumes/Oculto sí: fotos de verdad
        walks = [(self.root, "")]
        hidden_dirs = {(self.abs(self.hidden_prefix + "/x").parent, self.hidden_prefix),
                       (self.root / HIDDEN_DIR, HIDDEN_DIR)}          # la de dentro, por si quedó algo
        for start, prefix in hidden_dirs:
            if start.is_dir() and start != self.root:
                walks.append((start, prefix))
        for start, prefix in walks:
            self._scan_tree(start, prefix, out)
        return out

    def _scan_tree(self, start, prefix: str, out: dict) -> None:
        for dirpath, dirnames, filenames in os.walk(start):
            rel_dir = os.path.relpath(dirpath, start)
            rel_dir = "" if rel_dir == "." else rel_dir
            if prefix:
                rel_dir = f"{prefix}/{rel_dir}" if rel_dir else prefix
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and not (not prefix and rel_dir == "" and d in (self.upload_dir, ALBUMS_DIR))
                           and not is_excluded(os.path.join(rel_dir, d), self.excludes)]
            for name in filenames:
                rel = os.path.join(rel_dir, name) if rel_dir else name
                if name.startswith(".") or is_excluded(rel, self.excludes):
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name), follow_symlinks=False)
                except OSError:
                    continue
                if os.path.isfile(os.path.join(dirpath, name)) and not os.path.islink(os.path.join(dirpath, name)):
                    out[rel] = (st.st_size, st.st_mtime_ns)

    def _scan_inbox(self) -> list[tuple[str, int]]:
        """Ficheros de Subir/ listos para subir (sin tocar en los últimos min_age_s: aún copiándose)."""
        inbox = self.root / self.upload_dir
        if not self._dry:
            inbox.mkdir(exist_ok=True)
        if not inbox.is_dir():
            return []
        now, out = time.time(), []
        for dirpath, dirnames, filenames in os.walk(inbox):
            dirnames[:] = [d for d in dirnames if d not in (UPLOADED, REJECTED) and not d.startswith(".")
                           and not is_excluded(d, self.excludes)]
            for name in sorted(filenames):
                full = Path(dirpath) / name
                if name.startswith(".") or is_excluded(name, self.excludes) or full.is_symlink():
                    continue
                st = full.stat()
                if now - st.st_mtime < self.min_age_s:
                    continue
                out.append((str(full.relative_to(self.root)), st.st_size))
        return out

    def _local_only(self, known: dict[str, PhotoFile], local: dict) -> None:
        """Sin cambios en iCloud: solo lo que se decide con el NAS (ediciones/vídeos borrados a mano,
        ficheros modificados)."""
        for pf in known.values():
            if pf.dismissed:
                continue
            if pf.path not in local and pf.role != "original":
                self._dismiss(pf)
            elif pf.path in local and local[pf.path] != (pf.l_size, pf.l_mtime_ns):
                self._local_changed(pf, local[pf.path])

    def _dismiss(self, pf: PhotoFile) -> None:
        log.info("Fotos: %s se ha borrado en el NAS; no se volverá a descargar (iCloud no se toca)", pf.path)
        self._count("descartadas")
        if not self._dry:
            pf.dismissed = True
            self.db.put_file(pf)

    def _local_changed(self, pf: PhotoFile, stat: tuple[int, int]) -> None:
        if stat[0] != pf.l_size:
            self._result.warnings.append(f"{pf.path} ha cambiado en el NAS: Fotos no deja sustituir una foto, "
                                         "así que el cambio se queda solo en el NAS")
        if not self._dry:
            pf.l_size, pf.l_mtime_ns = stat
            self.db.put_file(pf)

    # --- plan -----------------------------------------------------------------------------------

    def _plan(self, assets, known, local, uploads, complete, gone: frozenset | set = frozenset(),
              folders: dict[str, str] | None = None):
        """`assets` puede ser solo una parte de la fototeca (carga inicial por bloques, cambios): entonces
        complete=False y lo borrado en iCloud llega en `gone` (ids que Apple dice que ya no están)."""
        by_asset: dict[str, dict[str, PhotoFile]] = {}
        for pf in known.values():
            by_asset.setdefault(pf.asset_id, {})[pf.role] = pf
        taken = set(known) | set(local)
        untracked = {p: s for p, s in local.items() if p not in known}
        downloads: list[tuple[RemoteAsset, RemoteFile, str, PhotoFile | None]] = []
        deletes: list[DeleteCandidate] = []

        for ra in assets.values():
            have = by_asset.get(ra.id, {})
            orig = have.get("original")
            if orig is not None and not orig.dismissed and orig.path not in local:
                moved = self._find_moved(orig, untracked)
                if moved is None:
                    deletes.append(DeleteCandidate("photo_icloud", orig.path, orig.size, "icloud", ra.id))
                    continue
                self._follow_move(orig, moved, have, local, untracked)
            ref = orig or next(iter(have.values()), None)
            if ref is not None:
                # Mismo nombre base que lo ya bajado de esta foto (p.ej. el vídeo bajó en un run anterior)
                folder, stem = os.path.split(os.path.splitext(ref.path)[0])
                if SUFFIX[ref.role] and stem.endswith(SUFFIX[ref.role]):
                    stem = stem[: -len(SUFFIX[ref.role])]
            else:
                folder = (folders or {}).get(ra.id) or ra.date.astimezone().strftime("%Y/%m")
                stem = self._free_stem(folder, os.path.splitext(ra.filename)[0], ra, taken)
            for role, rf in ra.files.items():
                pf = have.get(role)
                path = f"{folder}/{stem}{SUFFIX[role]}{os.path.splitext(rf.name)[1]}"
                if pf is None:
                    taken.add(path)
                    if role == "original" and ra.id in uploads:
                        self._place_upload(ra, rf, uploads[ra.id], path, have)
                    elif path in local and local[path][0] == rf.size:
                        self._adopt(ra, rf, path, local[path])
                    else:
                        downloads.append((ra, rf, path, None))
                elif pf.dismissed:
                    continue
                elif pf.path not in local:
                    if role != "original":
                        self._dismiss(pf)
                else:
                    remote_changed = (rf.fingerprint != pf.fingerprint) if (rf.fingerprint and pf.fingerprint) \
                        else rf.size != pf.size
                    local_changed = local[pf.path] != (pf.l_size, pf.l_mtime_ns)
                    if remote_changed and not local_changed:
                        downloads.append((ra, rf, pf.path, pf))
                    elif local_changed:
                        self._local_changed(pf, local[pf.path])
            for role, pf in have.items():
                if role not in ra.files and role != "original" and not pf.dismissed:
                    # Edición deshecha en el iPhone: la copia editada sobra
                    log.info("Fotos: %s ya no existe en iCloud (edición deshecha): a la papelera", pf.path)
                    self._count("a_papelera")
                    if not self._dry:
                        if pf.path in local:
                            self._trash(pf.path)
                        self.db.forget_file(pf.path)

        for asset_id, files in by_asset.items():
            if asset_id in assets or not (complete or asset_id in gone):
                continue
            if asset_id in getattr(self, "_hidden", ()) and asset_id not in gone:
                continue                       # oculta: no sale en "Todas las fotos", pero sigue ahí
            present = [pf for pf in files.values() if pf.path in local]
            main = files.get("original") or next(iter(files.values()))
            if not present:
                if not self._dry:
                    self.db.forget_asset(asset_id)    # borrada en los dos lados
                continue
            deletes.append(DeleteCandidate("photo_nas", main.path, sum(pf.size for pf in present), "nas", asset_id))
        return downloads, deletes

    @staticmethod
    def _find_moved(pf: PhotoFile, untracked: dict[str, tuple[int, int]]) -> str | None:
        name = os.path.basename(pf.path)
        hits = [p for p, (size, _) in untracked.items() if os.path.basename(p) == name and size == pf.l_size]
        return hits[0] if len(hits) == 1 else None

    def _follow_move(self, orig: PhotoFile, moved: str, have, local, untracked) -> None:
        log.info("Fotos: %s se ha movido en el NAS a %s; se sigue ahí", orig.path, moved)
        self._count("movidas_en_nas")
        untracked.pop(moved, None)
        if not self._dry:
            self.db.move_file(orig.path, moved)
        orig.path = moved
        # El resto de ficheros de la foto (vídeo, edición) se buscan igual; si no aparecen, se quedan
        for role, pf in have.items():
            if role != "original" and pf.path not in local:
                other = self._find_moved(pf, untracked)
                if other:
                    untracked.pop(other, None)
                    if not self._dry:
                        self.db.move_file(pf.path, other)
                    pf.path = other

    @staticmethod
    def _free_stem(folder: str, stem: str, ra: RemoteAsset, taken: set[str]) -> str:
        """Nombre base libre para TODOS los ficheros de la foto (IMG_1.HEIC + IMG_1.MOV + IMG_1 (editada).JPG)."""
        candidate, n = stem, 1
        while any(f"{folder}/{candidate}{SUFFIX[role]}{os.path.splitext(rf.name)[1]}" in taken
                  for role, rf in ra.files.items()):
            n += 1
            candidate = f"{stem} ({n})"
        return candidate

    def _adopt(self, ra: RemoteAsset, rf: RemoteFile, path: str, stat) -> None:
        """Ya está en el NAS con el mismo tamaño (p.ej. photos.db borrado): se enlaza sin bajarlo."""
        self._count("enlazadas")
        if not self._dry:
            self.db.put_file(PhotoFile(path, ra.id, rf.role, rf.size, rf.fingerprint, stat[0], stat[1]))

    def _place_upload(self, ra: RemoteAsset, rf: RemoteFile, src: str, path: str, have) -> None:
        log.info("Fotos: %s ya está en iCloud; se coloca en %s", src, path)
        self._count("colocadas")
        if self._dry:
            return
        dest = self.abs(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / src), str(dest))
        st = dest.stat()
        self.db.put_file(PhotoFile(path, ra.id, rf.role, rf.size, rf.fingerprint, st.st_size, st.st_mtime_ns))
        self.db.done_upload(ra.id)

    # --- subidas --------------------------------------------------------------------------------

    def _uploads(self, inbox: list[tuple[str, int]], known: dict[str, PhotoFile], budget: RunBudget) -> int:
        done = 0
        for rel, size in inbox:
            ext = os.path.splitext(rel)[1].lower()
            if ext not in UPLOAD_EXTENSIONS:
                self._result.warnings.append(f"{rel}: tipo de fichero que Fotos no admite; se deja en Subir/")
                continue
            if self._dry:
                log.info("Fotos (simulación): se subiría %s (%.1f MB)", rel, size / 2**20)
                self._count("subidas")
                continue
            if self.progress.stop_requested or not budget.allow("subidas", size):
                continue
            self.progress.action_start("upload", rel, size)
            try:
                asset_id, duplicate = self.remote.upload(self.root / rel)
            except AuthRequired:
                raise
            except Throttled as exc:
                self._unfinished += 1
                self.progress.action_done("upload", rel, size, False, str(exc))
                if budget.throttled(str(exc)):
                    break
                continue
            except Exception as exc:
                self.progress.action_done("upload", rel, size, False, str(exc))
                n = self.db.upload_failed(rel, f"{type(exc).__name__}: {exc}")
                self._result.errors.append(f"subir {rel}: {type(exc).__name__}: {exc}")
                if n >= 3:
                    dest = self._unique(Path(self.upload_dir) / REJECTED / os.path.basename(rel))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(self.root / rel, dest)
                    self.db.clear_upload_failure(rel)
                    self._result.warnings.append(f"{rel}: Apple la ha rechazado 3 veces; movida a "
                                                 f"{self.upload_dir}/{REJECTED}/")
                continue
            budget.spend("subidas", size)
            self.db.clear_upload_failure(rel)
            self.progress.action_done("upload", rel, size, True)
            if duplicate:
                # Apple no devuelve el id de la foto que ya tenía (comprobado con data/PRUEBA_FOTOS): no se
                # puede esperar a "colocarla". Ya está en iCloud y la sync la baja a su mes: esta copia sobra.
                log.info("Fotos: %s ya estaba en iCloud; la copia de Subir/ va a la papelera", rel)
                self.local.to_trash(rel)
                self._count("ya_estaban")
                continue
            dest = self._unique(Path(self.upload_dir) / UPLOADED / os.path.basename(rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.root / rel, dest)
            self.db.add_upload(asset_id, str(dest.relative_to(self.root)))
            self._count("subidas")
            done += 1
        return done

    def _unique(self, rel: Path) -> Path:
        dest, n = self.root / rel, 1
        while dest.exists():
            n += 1
            dest = self.root / rel.with_name(f"{rel.stem} ({n}){rel.suffix}")
        return dest

    # --- borrados -------------------------------------------------------------------------------

    def _decide_deletes(self, deletes: list[DeleteCandidate], decisions: dict):
        """Aplica lo decidido en el panel; lo nuevo, si son más de confirm_over, queda pendiente."""
        execute, restore, hold = [], [], []
        for c in deletes:
            d = decisions.get((c.kind, c.path))
            (execute if d == "confirmed" else restore if d == "rejected" else hold).append(c)
        in_batch = self.db.pending_keys()
        held = [c for c in hold if (c.kind, c.path) in in_batch]
        new = [c for c in hold if (c.kind, c.path) not in in_batch]
        if len(new) > self.confirm_over:
            held += new
        else:
            execute += new
        if held:
            self._result.status = "pending"
            self._result.pending_deletes = len(held)
            self._result.message = (f"{len(held)} fotos borradas esperan tu confirmación en el panel; "
                                    "el resto se sincroniza")
            if not self._dry:
                self.db.set_pending_batch([(c.kind, c.path, c.size, c.side) for c in held])
            else:
                log.info("Fotos (simulación): %d borrados quedarían pendientes de confirmar", len(held))
        elif not self._dry:
            self.db.clear_pending_batch()
        return execute, restore

    def _restore(self, c: DeleteCandidate, assets, known) -> list:
        """Borrado descartado en el panel."""
        if c.side == "icloud":
            # Lo borrado en el NAS vuelve a bajarse de iCloud a su sitio
            # Se olvida la foto ya: si la descarga no cabe en este run, el siguiente la ve como nueva y
            # la baja, en vez de tomarla otra vez por "borrada en el NAS" (y borrarla en iCloud).
            log.info("Fotos: se recupera %s desde iCloud", c.path)
            ra = assets[c.asset_id]
            files = {pf.role: pf for pf in known.values() if pf.asset_id == c.asset_id}
            if not self._dry:
                self.db.forget_asset(c.asset_id)
            return [(ra, rf, files[role].path, None) for role, rf in ra.files.items()
                    if role in files and not files[role].dismissed]
        # Borrada en iCloud: los ficheros del NAS se quedan y dejan de estar sincronizados
        log.info("Fotos: %s se conserva en el NAS (ya no está en iCloud; deja de sincronizarse)", c.path)
        if not self._dry:
            self.db.forget_asset(c.asset_id)
        return []

    def _run_deletes(self, execute: list[DeleteCandidate], known, budget: RunBudget) -> None:
        files_of: dict[str, list[PhotoFile]] = {}
        for pf in known.values():
            files_of.setdefault(pf.asset_id, []).append(pf)
        for c in execute:
            if self.progress.stop_requested or budget.exhausted:
                break
            if not budget.allow("borrados"):
                continue
            self.progress.action_start("delete", c.path, 0)
            try:
                if c.side == "icloud":
                    self.remote.delete(c.asset_id)
                    log.info("Fotos: %s borrada en el NAS → borrada en iCloud (Eliminados recientemente)", c.path)
                    self._count("borradas_en_icloud")
                else:
                    log.info("Fotos: %s borrada en iCloud → a la papelera del NAS", c.path)
                    self._count("a_papelera")
                for pf in files_of.get(c.asset_id, []):
                    if self.abs(pf.path).exists():
                        self._trash(pf.path)
                self.db.forget_asset(c.asset_id)
                budget.spend("borrados")
                self.progress.action_done("delete", c.path, 0, True)
            except AuthRequired:
                raise
            except Throttled as exc:
                self._unfinished += 1
                self.progress.action_done("delete", c.path, 0, False, str(exc))
                if budget.throttled(str(exc)):
                    break
            except Exception as exc:
                self.progress.action_done("delete", c.path, 0, False, str(exc))
                self._result.errors.append(f"borrar {c.path}: {type(exc).__name__}: {exc}")

    # --- descargas ------------------------------------------------------------------------------

    def _refresh_stale(self, downloads, i: int) -> None:
        """Si la URL de la descarga i está caducando, renueva de una vez las de las siguientes (hasta
        LOOKUP_BATCH fotos): 2 peticiones por bloque en vez de 2 por foto."""
        rf = downloads[i][1]
        if not rf.listed_at or time.time() - rf.listed_at < URL_MAX_AGE_S - 60 or not hasattr(self.remote, "refresh_urls"):
            return
        batch, ids = [], set()
        for ra, f, _p, _o in downloads[i:]:
            if ra.id not in ids and len(ids) >= LOOKUP_BATCH:
                break
            ids.add(ra.id)
            batch.append((ra.id, f))
        n = self.remote.refresh_urls(batch)
        log.info("Fotos: URL renovadas para %d ficheros (%d fotos)", n, len(ids))

    def _retry_due(self) -> bool:
        now = time.time()
        return any(now - t >= min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (n - 1))
                   for n, t in self.db.download_fails().values())

    def _order_downloads(self, downloads):
        """Primero las que nunca han fallado (lo más reciente primero, como venían); después las que ya han
        fallado, en orden aleatorio y solo si ha pasado su espera (30 min × 2^fallos, tope 24 h). Así un fichero
        que Apple sirve a goteo o que da error no se come cada vuelta el cupo y el tiempo, y por repetición
        se llega a todos los demás."""
        fails = self.db.download_fails()
        if not fails:
            return downloads
        now = time.time()
        fresh, retry = [], []
        for d in downloads:
            f = fails.get((d[0].id, d[1].role))
            if f is None:
                fresh.append(d)
            elif now - f[1] >= min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (f[0] - 1)):
                retry.append(d)
        random.shuffle(retry)
        waiting = len(downloads) - len(fresh) - len(retry)
        if waiting:
            log.info("Fotos: %d ficheros esperan antes de reintentarse (fallaron hace poco)", waiting)
        return fresh + retry

    def _run_downloads(self, downloads, budget: RunBudget) -> None:
        downloads = self._order_downloads(downloads)
        for i, (ra, rf, path, old) in enumerate(downloads):
            if self._streak >= MAX_CONSECUTIVE_ERRORS:
                self._unfinished += 1
                if self._result.status == "ok":
                    self._result.status = "partial"
                    self._result.message = (f"{self._streak} descargas seguidas han fallado; se corta la vuelta para no "
                                            f"insistir a Apple. Último error: {self._result.errors[-1] if self._result.errors else '?'}")
                return
            if self.progress.stop_requested:
                self._result.status = "stopped"
                self._result.message = "Parada a petición del usuario; el siguiente run continúa donde se quedó"
                return
            if budget.exhausted:
                return
            if not budget.allow("descargas", rf.size):
                continue
            try:
                self._refresh_stale(downloads, i)
            except (AuthRequired, Throttled):
                raise
            except Exception as exc:
                log.info("Fotos: no se pudieron renovar las URL (%s); se intenta con las que hay", exc)
            dest = self.abs(path)
            self.progress.action_start("download", path, rf.size)
            try:
                if old is not None and dest.exists():
                    # Edición nueva en el iPhone: la versión anterior se guarda en la papelera
                    trash = self.local.trash_dir / datetime.now().strftime("%Y-%m-%d") / "Fotos" / path
                    trash.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, trash)
                written = self.remote.download(rf, dest, ra.date.timestamp(), asset_id=ra.id)
                st = dest.stat()
                self.db.put_file(PhotoFile(path, ra.id, rf.role, rf.size or written, rf.fingerprint,
                                           st.st_size, st.st_mtime_ns))
                budget.spend("descargas", written)
                self.db.download_ok(ra.id, rf.role)
                self._streak = 0
                self._count("descargas_editadas" if old is not None else "descargas")
                self.progress.action_done("download", path, rf.size, True)
            except AuthRequired:
                raise
            except Throttled as exc:
                self._unfinished += 1
                self.progress.action_done("download", path, rf.size, False, str(exc))
                if budget.throttled(str(exc)):
                    return
            except Exception as exc:
                self.progress.action_done("download", path, rf.size, False, str(exc))
                self._result.errors.append(f"descargar {path}: {type(exc).__name__}: {exc}")
                budget.spend("descargas")        # un intento fallido también es una petición a Apple
                n = self.db.download_failed(ra.id, rf.role, f"{type(exc).__name__}: {exc}", time.time())
                if n >= 3:
                    log.warning("Fotos: %s lleva %d intentos fallidos (%s)", path, n, exc)
                self._streak += 1

    # --- utilidades -----------------------------------------------------------------------------

    def _count(self, kind: str, n: int = 1) -> None:
        self._result.counts[kind] = self._result.counts.get(kind, 0) + n

    def _log_plan(self, downloads, execute) -> None:
        by_role: dict[str, list] = {}
        for ra, rf, path, old in downloads:
            by_role.setdefault(rf.role, []).append(rf.size)
        for role, sizes in sorted(by_role.items()):
            log.info("Fotos (simulación): se descargarían %d ficheros '%s' (%.1f GB)", len(sizes), role,
                     sum(sizes) / 2**30)
        for ra, rf, path, old in downloads[:10]:
            log.info("Fotos (simulación): p.ej. %s (%.1f MB)", path, rf.size / 2**20)
        for c in execute[:20]:
            log.info("Fotos (simulación): se borraría %s %s", c.path,
                     "en iCloud" if c.side == "icloud" else "del NAS (a la papelera)")
        n_lim = min(len(downloads), self.limits.max_downloads)
        self._count("descargas", n_lim)
        if len(downloads) > n_lim:
            log.info("Fotos (simulación): con el límite de %d por run, harían falta unos %d runs para la descarga "
                     "completa", self.limits.max_downloads, -(-len(downloads) // self.limits.max_downloads))
        self._count("borrados", len(execute))
