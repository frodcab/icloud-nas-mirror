# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Hilo de la sync de iCloud Fotos: aparte del de Drive (la primera descarga dura días y no puede
frenar a Drive), con la MISMA sesión de Apple. Si la sesión no está bien, espera: la reconexión y
el 2FA los lleva el hilo de Drive."""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from services.entries import AuthRequired, RunResult, SyncAborted
from services.notify import EmailNotifier
from services.photos_db import PhotosDB
from services.photos_engine import PhotosEngine
from services.photos_limits import Backoff
from services.progress import Progress
from services.watchdog import WatchdogMixin
from services.scheduler import summarize_pending

log = logging.getLogger(__name__)


class PhotosSyncService(WatchdogMixin):
    def __init__(self, *, db: PhotosDB, auth, engine: PhotosEngine, notifier: EmailNotifier,
                 interval_min: int, dry_run: bool, progress: Progress, public_url: str = "",
                 force_dry_run_files: list[Path] | None = None, start_delay_s: float = 60,
                 stall_s: float = 1200, kill_grace_s: float = 300, watch_every_s: float = 30,
                 exit_fn=None, run_max_s: float = 10800):
        self.db, self.auth, self.engine, self.notifier = db, auth, engine, notifier
        self.interval_s = interval_min * 60
        self.dry_run = dry_run
        self.progress = progress
        self.public_url = public_url
        self.force_dry_run_files = force_dry_run_files or []
        self.backoff = Backoff()
        self.running = False
        self.last_budget: str = ""
        # Al arrancar se deja empezar antes a Drive (que es quien conecta con Apple)
        self.next_run_at = time.time() + start_delay_s
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._pending_dry_run: bool | None = None
        self._rerun = False
        self._stop = False
        self.init_watchdog("Fotos", stall_s=stall_s, kill_grace_s=kill_grace_s, run_max_s=run_max_s,
                           watch_every_s=watch_every_s, exit_fn=exit_fn)

    # --- hilo -----------------------------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._loop, name="photos-loop", daemon=True).start()
        self.start_watchdog()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def trigger(self, dry_run: bool | None = None) -> bool:
        if self.running:
            self._rerun = True
            return False
        self._pending_dry_run = dry_run
        self._wake.set()
        return True

    def request_stop(self) -> bool:
        if not self.running:
            return False
        self.progress.stop_requested = True
        return True

    def forced_dry_run(self) -> bool:
        return any(f.exists() for f in self.force_dry_run_files)

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=max(0.0, self.next_run_at - time.time()))
            if self._stop:
                return
            self._wake.clear()
            dry_run = self.dry_run if self._pending_dry_run is None else self._pending_dry_run
            self._pending_dry_run = None
            try:
                self.run_once(dry_run)
            except Exception:
                log.exception("Unexpected error in the Photos loop")
            wait = 0 if self._rerun else self.interval_s * self.backoff.factor
            if self.backoff.factor > 1 and not self._rerun:
                log.info("Photos: Apple slowed us down; the next run waits %d min", wait // 60)
            self.next_run_at = time.time() + wait
            self._rerun = False

    # --- un run ---------------------------------------------------------------------------------

    def run_once(self, dry_run: bool) -> RunResult:
        if self.auth.state != "ok":
            log.info("Photos paused: iCloud session in state %s", self.auth.state)
            return RunResult(status="skipped", message="Esperando a la sesión de Apple")
        if not self._run_lock.acquire(blocking=False):
            return RunResult(status="skipped", message="Ya hay una sync de Fotos en marcha")
        if self.forced_dry_run():
            dry_run = True
        self.running = True
        self.progress.start(dry_run)
        run_id = self.db.start_run(dry_run)
        started = time.time()
        log.info("Photos sync started%s", " (simulation)" if dry_run else "")
        budget = None
        try:
            result, budget = self.engine.run(dry_run=dry_run)
        except AuthRequired as exc:
            log.warning("Photos: Apple session expired (%s); it will retry when it is back", exc)
            result = RunResult(status="error", dry_run=dry_run, message=f"Sesión de iCloud caducada ({exc})")
        except SyncAborted as exc:
            log.error("Photos sync aborted: %s", exc)
            result = RunResult(status="aborted", dry_run=dry_run, message=str(exc))
        except Exception as exc:
            log.exception("Photos sync failed")
            result = RunResult(status="error", dry_run=dry_run, message=f"{type(exc).__name__}: {exc}")
        finally:
            totals = self.progress.totals()
            self.progress.finish()
            self.running = False
            self._run_lock.release()
        if budget is not None:
            self.backoff.after_run(budget)
            self.last_budget = budget.summary()
        for w in result.warnings[:50]:
            log.info("Fotos, aviso: %s", w)
        self.db.finish_run(run_id, result.status, result.message, result.counts,
                           result.errors[:200] + [f"aviso: {w}" for w in result.warnings[:50]],
                           bytes_down=totals["bytes_down"], bytes_up=totals["bytes_up"],
                           duration_s=round(time.time() - started, 1))
        self._notify(result)
        return result

    def _notify(self, result: RunResult) -> None:
        link = f"\nPanel: {self.public_url}" if self.public_url else ""
        failing = self.db.get("failing") is not None
        if result.status in ("error", "aborted") and "caducada" not in result.message:
            if not failing:
                self.db.set("failing", datetime.now().isoformat(timespec="seconds"))
                self.notifier.send("Fotos: la sincronización ha fallado",
                                   f"{result.message}\n\nNo se volverá a avisar hasta que se recupere.{link}")
        elif result.status in ("ok", "partial", "pending", "stopped") and failing:
            self.db.set("failing", None)
            self.notifier.send("Fotos: sincronización recuperada", f"Vuelve a funcionar.\n\n{result.message}")
        if result.status == "pending" and not result.dry_run:
            summary = self.pending_summary()
            if summary and self.db.get("pending_notified") != summary["batch_id"]:
                self.db.set("pending_notified", summary["batch_id"])
                self.notifier.send(f"Fotos: {summary['files']} fotos borradas esperan tu confirmación",
                                   f"{summary['text']}\n\nNo se ha borrado nada todavía; el resto sigue "
                                   f"sincronizándose. Confírmalo o descártalo en el panel (pestaña Fotos).{link}")

    # --- panel ----------------------------------------------------------------------------------

    def pending_summary(self) -> dict | None:
        return summarize_pending(self.db, noun="fotos")

    def decide_group(self, batch_id: str, folder: str, side: str, decision: str) -> int:
        summary = self.pending_summary()
        if not summary or summary["batch_id"] != batch_id:
            return 0
        group = next((g for g in summary["folders"] if g["folder"] == folder and g["side"] == side), None)
        if group is None:
            return 0
        return self.db.decide_paths(batch_id, [(i["kind"], i["path"]) for i in group["items"]], decision)

    def status(self) -> dict:
        n, batch = self.db.pending_info()
        return {
            "running": self.running,
            "dry_run": self.dry_run,
            "forced_dry_run": self.forced_dry_run(),
            "next_run_at": datetime.fromtimestamp(self.next_run_at).isoformat(timespec="seconds"),
            "backoff": self.backoff.factor,
            "failing_since": self.db.get("failing"),
            "pending_deletes": n,
            "pending_batch": batch,
            "last_budget": self.last_budget,
            "quiet_s": round(time.time() - self.progress.last_activity) if self.running else 0,
            "restarted_by_watchdog": self.db.get("restarted_by_watchdog"),
            "stats": self.db.stats(),
            "mode": self.engine.mode(),
            "backfill_done_upto": int(self.db.get("backfill_k") or 0),
            "backfill_total": int(self.db.get("backfill_total") or 0),
        }
