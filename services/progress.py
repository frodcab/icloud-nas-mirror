# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Estado en vivo del run en curso: fase, contadores, bytes, feed de acciones y log.

Lo escribe el hilo de sync y lo leen las peticiones del panel (/api/progress, /api/activity,
/api/log): todo pasa por un lock y snapshot() devuelve copias.
"""

import threading
import time
from collections import deque

PHASES = ["idle", "connecting", "listing_remote", "scanning_local", "planning", "transferring", "apps", "heic"]
TRANSFER_KINDS = {"download", "upload", "replace", "conflict"}


class Progress:
    def __init__(self, activity_size: int = 300, log_size: int = 5000):
        self._lock = threading.Lock()
        self._activity: deque[dict] = deque(maxlen=activity_size)
        self._log: deque[tuple[int, str]] = deque(maxlen=log_size)
        self._seq = 0
        self._log_seq = 0
        self.stop_requested = False
        self.last_activity = time.time()    # latido: lo lee el vigilante de la sync de Fotos
        self._reset()

    def _reset(self) -> None:
        self.phase = "idle"
        self.dry_run = False
        self.started_at: float | None = None
        self.folders_scanned = 0
        self.files_seen = 0
        self.current_folder = ""
        self.actions_total = 0
        self.actions_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.bytes_down = 0
        self.bytes_up = 0
        self.current: dict | None = None
        self.counts_done: dict[str, int] = {}
        self.errors = 0
        self._speed_samples: deque[tuple[float, int]] = deque(maxlen=30)

    # --- escritura (hilo de sync) ---------------------------------------------------------------

    def touch(self) -> None:
        """Marca de vida: cualquier avance del run (página leída, trozo bajado, llamada a Apple que vuelve)."""
        self.last_activity = time.time()

    def start(self, dry_run: bool) -> None:
        self.last_activity = time.time()
        with self._lock:
            self._reset()
            self.stop_requested = False
            self.dry_run = dry_run
            self.started_at = time.time()
            self.phase = "connecting"
            self._log.clear()

    def set_phase(self, phase: str) -> None:
        self.last_activity = time.time()
        with self._lock:
            self.phase = phase
            if phase == "transferring":
                self._speed_samples.append((time.time(), self.bytes_done))

    def finish(self) -> None:
        with self._lock:
            self.phase = "idle"
            self.current = None
            self.current_folder = ""

    def folder_scanned(self, path: str, n_items: int) -> None:
        self.last_activity = time.time()
        with self._lock:
            self.folders_scanned += 1
            self.files_seen += n_items
            self.current_folder = path or "/"

    def set_plan(self, actions_total: int, bytes_total: int) -> None:
        """Totales de una etapa (sync principal, luego copia de apps): la barra empieza de cero.
        bytes_down/bytes_up y counts_done siguen acumulando todo el run."""
        self.last_activity = time.time()
        with self._lock:
            self.actions_total = actions_total
            self.bytes_total = bytes_total
            self.actions_done = 0
            self.bytes_done = 0
            self._speed_samples.clear()

    def action_start(self, kind: str, path: str, size: int) -> None:
        self.last_activity = time.time()
        with self._lock:
            self.current = {"kind": kind, "path": path, "size": size, "done": 0}

    def add_bytes(self, n: int, upload: bool = False) -> None:
        self.last_activity = time.time()
        with self._lock:
            self.bytes_done += n
            if upload:
                self.bytes_up += n
            else:
                self.bytes_down += n
            if self.current is not None:
                self.current["done"] += n
            self._speed_samples.append((time.time(), self.bytes_done))

    def action_done(self, kind: str, path: str, size: int, ok: bool, error: str = "", reason: str = "") -> None:
        self.last_activity = time.time()
        with self._lock:
            self.actions_done += 1
            if ok:
                self.counts_done[kind] = self.counts_done.get(kind, 0) + 1
            else:
                self.errors += 1
            self.current = None
            self._seq += 1
            self._activity.append({
                "seq": self._seq, "ts": time.time(), "kind": kind, "path": path, "size": size,
                "ok": ok, "error": error, "reason": reason,
            })

    def add_log(self, line: str) -> None:
        self.last_activity = time.time()
        with self._lock:
            self._log_seq += 1
            self._log.append((self._log_seq, line))

    # --- lectura (panel) ------------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            speed = 0.0
            if len(self._speed_samples) >= 2:
                (t0, b0), (t1, b1) = self._speed_samples[0], self._speed_samples[-1]
                if t1 - t0 > 0.5:
                    speed = (b1 - b0) / (t1 - t0)
            # Si el último bloque es antiguo (fichero pequeño lento, listado...), la velocidad cae a 0
            if self._speed_samples and now - self._speed_samples[-1][0] > 20:
                speed = 0.0
            remaining = max(0, self.bytes_total - self.bytes_done)
            eta = remaining / speed if speed > 0 and self.phase == "transferring" else None
            return {
                "phase": self.phase,
                "running": self.phase != "idle",
                "dry_run": self.dry_run,
                "stop_requested": self.stop_requested,
                "started_at": self.started_at,
                "elapsed_s": round(now - self.started_at, 1) if self.started_at and self.phase != "idle" else None,
                "folders_scanned": self.folders_scanned,
                "files_seen": self.files_seen,
                "current_folder": self.current_folder,
                "actions_total": self.actions_total,
                "actions_done": self.actions_done,
                "bytes_total": self.bytes_total,
                "bytes_done": self.bytes_done,
                "speed_bps": round(speed),
                "eta_s": round(eta) if eta is not None else None,
                "current": dict(self.current) if self.current else None,
                "counts_done": dict(self.counts_done),
                "errors": self.errors,
                "activity_seq": self._seq,
                "log_seq": self._log_seq,
            }

    def activity(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._activity if a["seq"] > since]

    def log_lines(self, since: int = 0) -> list[tuple[int, str]]:
        with self._lock:
            return [item for item in self._log if item[0] > since]

    def totals(self) -> dict:
        with self._lock:
            return {"bytes_down": self.bytes_down, "bytes_up": self.bytes_up}
