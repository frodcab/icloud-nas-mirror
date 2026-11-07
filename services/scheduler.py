# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Hilo de fondo que lanza la sync cada SYNC_INTERVAL_MIN y centraliza estado y avisos."""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from services.entries import AuthRequired, RunResult, SyncAborted
from services.icloud_client import ICloudAuth
from services.local_fs import LocalTree
from services.notify import EmailNotifier
from services.progress import Progress
from services.state_db import StateDB
from services.sync_engine import SyncEngine
from services.watchdog import WatchdogMixin

log = logging.getLogger(__name__)


AUTH_WAIT_TEXT = {
    "needs_2fa": "En pausa: esperando el código de verificación de Apple",
    "error": "En pausa: error con la cuenta de Apple",
    "disconnected": "En pausa: no se pudo conectar con Apple, se reintenta en la próxima",
    "not_configured": "En pausa: falta configurar la cuenta de Apple",
}


class _RunLogCapture(logging.Handler):
    """Copia el log del run a una lista (para last_run.log) y al log en vivo del panel."""

    def __init__(self, progress: Progress):
        super().__init__(logging.INFO)
        self.lines: list[str] = []
        self.progress = progress
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        line = self.format(record)
        if len(self.lines) < 20000:
            self.lines.append(line)
        self.progress.add_log(line)


class SyncService(WatchdogMixin):
    def __init__(self, *, db: StateDB, auth: ICloudAuth, engine: SyncEngine, local: LocalTree,
                 notifier: EmailNotifier, interval_min: int, dry_run: bool, trash_days: int,
                 state_dir: Path, public_url: str, progress: Progress, apps=None, heic=None,
                 force_dry_run_files: list[Path] | None = None, photos_probe=None,
                 stall_s: float = 2700, kill_grace_s: float = 300, run_max_s: float = 21600, exit_fn=None):
        self.force_dry_run_files = force_dry_run_files or []
        self.photos_probe = photos_probe
        self.db, self.auth, self.engine, self.local = db, auth, engine, local
        self.apps = apps
        self.heic = heic
        self.progress = progress
        self.notifier = notifier
        self.interval_s = interval_min * 60
        self.dry_run = dry_run
        self.trash_days = trash_days
        self.log_path = state_dir / "last_run.log"
        self.public_url = public_url
        self.running = False
        self.next_run_at: float = time.time()
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._pending_dry_run: bool | None = None
        self._stop = False
        auth.on_change = self._on_auth_change
        self.init_watchdog("Drive", stall_s=stall_s, kill_grace_s=kill_grace_s, run_max_s=run_max_s, exit_fn=exit_fn)

    # --- hilo -----------------------------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._loop, name="sync-loop", daemon=True).start()
        self.start_watchdog()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def trigger(self, dry_run: bool | None = None) -> bool:
        """Pide un run inmediato. Si ya hay uno en marcha devuelve False, pero deja encargado otro
        para justo cuando termine (p.ej. para aplicar un borrado recién confirmado)."""
        if self.running:
            self._rerun = True
            return False
        self._pending_dry_run = dry_run
        self._wake.set()
        return True

    def forced_dry_run(self) -> bool:
        return any(f.exists() for f in self.force_dry_run_files)

    def request_stop(self) -> bool:
        """Pide parar el run en curso al acabar el fichero actual. False si no hay run."""
        if not self.running:
            return False
        self.progress.stop_requested = True
        return True

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=max(0.0, self.next_run_at - time.time()))
            if self._stop:
                return
            self._wake.clear()
            dry_run = self.dry_run if self._pending_dry_run is None else self._pending_dry_run
            if self.forced_dry_run():
                dry_run = True
            self._pending_dry_run = None
            try:
                self.run_once(dry_run)
            except Exception:
                log.exception("Error inesperado en el bucle de sync")
            self.next_run_at = time.time() + (0 if getattr(self, "_rerun", False) else self.interval_s)
            self._rerun = False

    # --- un run ---------------------------------------------------------------------------------

    def run_once(self, dry_run: bool) -> RunResult:
        if not self._run_lock.acquire(blocking=False):
            return RunResult(status="skipped", message="Ya hay una sync en marcha")
        if self.forced_dry_run():
            dry_run = True        # interruptor de emergencia: gana a cualquier otra cosa
        self.running = True
        self.progress.start(dry_run)
        capture = _RunLogCapture(self.progress)
        logging.getLogger().addHandler(capture)
        run_id = self.db.start_run(dry_run)
        started = time.time()
        try:
            result = self._run(dry_run)
        finally:
            logging.getLogger().removeHandler(capture)
            totals = self.progress.totals()
            self.progress.finish()
            self.running = False
            self._run_lock.release()
        self.db.finish_run(run_id, result.status, result.message, result.counts,
                           result.errors + [f"aviso: {w}" for w in result.warnings],
                           bytes_down=totals["bytes_down"], bytes_up=totals["bytes_up"],
                           duration_s=round(time.time() - started, 1))
        try:
            self.log_path.write_text("\n".join(capture.lines) + "\n", encoding="utf-8")
        except OSError:
            log.warning("No se pudo guardar %s", self.log_path)
        self._notify_result(result)
        return result

    def _run(self, dry_run: bool) -> RunResult:
        log.info("Inicio de sync%s", " (simulación)" if dry_run else "")
        self._purge_trash_daily()
        if self.auth.state == "disconnected":
            self.auth.connect()
        if self.auth.state != "ok":
            log.info("Sync en pausa: sesión de iCloud en estado %s. %s", self.auth.state, self.auth.message)
            return RunResult(status="skipped", dry_run=dry_run,
                             message=AUTH_WAIT_TEXT.get(self.auth.state, self.auth.state))
        if self.photos_probe is not None:
            self.photos_probe.run()       # sonda de iCloud Fotos (fase 0); nunca lanza
        try:
            result = self.engine.run(dry_run=dry_run)
            if self.apps is not None and result.status != "stopped":
                self._run_apps(result, dry_run)
            if self.heic is not None and result.status != "stopped":
                self._merge(result, self.heic.run(dry_run=dry_run))
        except AuthRequired as exc:
            log.warning("Sesión caducada durante la sync: %s", exc)
            # Con las cookies de confianza Apple suele dejar entrar sin 2FA; si no, pasa a needs_2fa.
            self.auth.connect()
            return RunResult(status="error", dry_run=dry_run,
                             message=f"Sesión de iCloud caducada ({exc}); estado ahora: {self.auth.state}")
        except SyncAborted as exc:
            log.error("Sync abortada: %s", exc)
            return RunResult(status="aborted", dry_run=dry_run, message=str(exc))
        except Exception as exc:
            log.exception("Sync fallida")
            return RunResult(status="error", dry_run=dry_run, message=f"{type(exc).__name__}: {exc}")
        done = sum(result.counts.values())
        result.message = result.message or (
            f"{done} operaciones, {len(result.errors)} errores" if done or result.errors
            else "Sin cambios"
        )
        for w in result.warnings:
            log.info("Aviso: %s", w)
        log.info("Fin de sync: %s %s", result.status, result.counts)
        return result

    def _run_apps(self, result: RunResult, dry_run: bool) -> None:
        """Copia de las App Libraries (solo iCloud → NAS). Sus fallos no tumban la sync principal:
        se suman como errores del run."""
        try:
            apps = self.apps.run(dry_run=dry_run)
        except SyncAborted as exc:
            log.error("Copia de las apps abortada: %s", exc)
            result.errors.append(f"Copia de las apps abortada: {exc}")
            result.status = "partial"
            return
        self._merge(result, apps)

    @staticmethod
    def _merge(result: RunResult, stage: RunResult) -> None:
        """Suma una etapa extra (apps, HEIC→JPG) al resultado del run. Sus fallos no lo tumban."""
        for kind, n in stage.counts.items():
            result.counts[kind] = result.counts.get(kind, 0) + n
        result.errors.extend(stage.errors)
        result.warnings.extend(stage.warnings)
        if stage.status == "stopped":
            result.status = "stopped"
            result.message = "Parada a petición del usuario; el siguiente run continúa donde se quedó"
        elif result.status == "ok" and stage.errors:
            result.status = "partial"

    def _purge_trash_daily(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.db.get("trash_purged_on") != today:
            removed = self.local.purge_trash(self.trash_days)
            if removed:
                log.info("Papelera local: purgados %d días con más de %d días", removed, self.trash_days)
            self.db.set("trash_purged_on", today)

    # --- avisos ---------------------------------------------------------------------------------

    def _notify_result(self, result: RunResult) -> None:
        failing = self.db.get("failing") is not None
        if result.status in ("error", "aborted"):
            if not failing:  # solo el primer fallo, no cada reintento
                self.db.set("failing", datetime.now().isoformat(timespec="seconds"))
                self.notifier.send(
                    "La sincronización ha fallado" if result.status == "error" else "Sincronización abortada",
                    f"{result.message}\n\nNo se volverá a avisar hasta que se recupere.\n{self._ui_link()}",
                )
        elif result.status in ("ok", "partial", "stopped", "pending") and failing:
            self.db.set("failing", None)
            self.notifier.send("Sincronización recuperada", f"Vuelve a funcionar.\n\n{result.message}")
        if result.status == "pending" and not result.dry_run:
            summary = self.pending_summary()
            if summary and self.db.get("pending_notified") != summary["batch_id"]:
                self.db.set("pending_notified", summary["batch_id"])
                folders = "\n".join(f"  - {f['folder'] or '/'}: {f['count']} ({f['side_text']})"
                                     for f in summary["folders"][:15])
                self.notifier.send(
                    f"{summary['files']} ficheros borrados esperan tu confirmación",
                    f"{summary['text']}\n\n{folders}\n\nNo se ha borrado nada todavía; el resto sigue "
                    f"sincronizándose. Confírmalo o descártalo en el panel.\n{self._ui_link()}",
                )
        if result.conflicts:
            self.notifier.send(
                f"{len(result.conflicts)} conflicto(s)",
                "Estos ficheros cambiaron en el NAS y en iCloud a la vez. Se han conservado las dos "
                "versiones: la de iCloud con el nombre original y la del NAS como copia.\n\n"
                + "\n".join(result.conflicts) + f"\n\n{self._ui_link()}",
            )

    def _on_auth_change(self, state: str, message: str) -> None:
        if state == "needs_2fa":
            self.notifier.send("Apple pide el código de verificación",
                               f"{message}\n\nLa sincronización está en pausa hasta entonces.\n{self._ui_link()}")
        elif state == "error":
            self.notifier.send("Error de sesión con Apple", f"{message}\n{self._ui_link()}")

    def _ui_link(self) -> str:
        return f"Panel: {self.public_url}" if self.public_url else ""

    def pending_summary(self) -> dict | None:
        return summarize_pending(self.db)

    def decide_group(self, batch_id: str, folder: str, side: str, decision: str) -> int:
        """Confirma o descarta solo una subcarpeta del lote (tal como la agrupa pending_summary)."""
        summary = self.pending_summary()
        if not summary or summary["batch_id"] != batch_id:
            return 0
        group = next((g for g in summary["folders"] if g["folder"] == folder and g["side"] == side), None)
        if group is None:
            return 0
        return self.db.decide_paths(batch_id, [(i["kind"], i["path"]) for i in group["items"]], decision)

    def status(self) -> dict:
        return {
            "auth_state": self.auth.state,
            "auth_message": self.auth.message,
            "two_factor_delivery": self.auth.delivery,
            "running": self.running,
            "next_run_at": datetime.fromtimestamp(self.next_run_at).isoformat(timespec="seconds"),
            "failing_since": self.db.get("failing"),
            "forced_dry_run": self.forced_dry_run(),
            "pending_deletes": self.db.pending_info()[0],
            "pending_batch": self.db.pending_info()[1],
        }


def summarize_pending(db, noun: str = "ficheros") -> dict | None:
    """Lote de borrados pendiente, agrupado por la carpeta más alta afectada."""
    items = db.pending_items()
    if not items:
        return None
    dirs = {i["path"] for i in items if i["kind"].startswith("rmdir")}

    def group(path: str) -> str:
        top, parent = None, path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in dirs:
                top = parent
        if path in dirs and top is None:
            return path
        return top or (path.rsplit("/", 1)[0] if "/" in path else "")

    side_text = {"icloud": "se borrarían en iCloud → Eliminados recientemente",
                 "nas": "irían a la papelera del NAS"}
    groups: dict[tuple[str, str], dict] = {}
    for i in items:
        key = (group(i["path"]), i["side"])
        g = groups.setdefault(key, {"folder": key[0], "side": key[1], "side_text": side_text[key[1]],
                                    "count": 0, "bytes": 0, "items": []})
        g["items"].append({"kind": i["kind"], "path": i["path"], "size": i["size"]})
        if not i["kind"].startswith("rmdir"):
            g["count"] += 1
            g["bytes"] += i["size"]
    sides = {s: {"count": sum(1 for i in items if i["side"] == s and not i["kind"].startswith("rmdir")),
                 "bytes": sum(i["size"] for i in items if i["side"] == s)} for s in ("icloud", "nas")}
    parts = []
    if sides["icloud"]["count"] or any(i["side"] == "icloud" for i in items):
        parts.append(f"Has borrado {sides['icloud']['count']} {noun} en el NAS: se borrarían también en "
                     "iCloud (van a Eliminados recientemente, recuperables 30 días)")
    if sides["nas"]["count"] or any(i["side"] == "nas" for i in items):
        parts.append(f"Se han borrado {sides['nas']['count']} {noun} en iCloud: se moverían a la "
                     "papelera del NAS")
    return {
        "batch_id": items[0]["batch_id"], "created_at": items[0]["created_at"], "total": len(items),
        "files": sides["icloud"]["count"] + sides["nas"]["count"],
        "dirs": sum(1 for i in items if i["kind"].startswith("rmdir")),
        "sides": sides, "text": ". ".join(parts) + ".",
        "folders": sorted(groups.values(), key=lambda g: (-g["count"], g["folder"])),
        "items": items,
    }
