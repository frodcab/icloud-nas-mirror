# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Estado persistente en SQLite (WAL): snapshot de la última sync, historial de runs y conflictos."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from services.entries import SnapshotEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    path TEXT PRIMARY KEY,
    is_dir INTEGER NOT NULL,
    etag TEXT,
    r_size INTEGER NOT NULL DEFAULT 0,
    r_mtime REAL NOT NULL DEFAULT 0,
    l_size INTEGER NOT NULL DEFAULT 0,
    l_mtime_ns INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    counts TEXT,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    conflict_copy TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_deletes (
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    side TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (kind, path)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StateDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            # Columnas añadidas después de la primera versión: se crean si faltan, sin tocar los datos
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
            for col, ddl in (("bytes_down", "INTEGER NOT NULL DEFAULT 0"),
                             ("bytes_up", "INTEGER NOT NULL DEFAULT 0"),
                             ("duration_s", "REAL")):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
            # Un run sin terminar al arrancar es uno que cortó un reinicio o redespliegue
            self._conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=started_at, "
                "message='Cortada por un reinicio de la app; la siguiente continúa donde se quedó' "
                "WHERE finished_at IS NULL"
            )

    # --- snapshot -------------------------------------------------------------

    def load_snapshot(self) -> dict[str, SnapshotEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, is_dir, etag, r_size, r_mtime, l_size, l_mtime_ns FROM snapshot"
            ).fetchall()
        return {r[0]: SnapshotEntry(r[0], bool(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows}

    def put(self, e: SnapshotEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?)",
                (e.path, int(e.is_dir), e.etag, e.r_size, e.r_mtime, e.l_size, e.l_mtime_ns),
            )

    def forget(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM snapshot WHERE path = ?", (path,))

    def rename_prefix(self, old: str, new: str) -> None:
        """Mueve en el snapshot una carpeta y todo lo que cuelga de ella (renombrado)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, is_dir, etag, r_size, r_mtime, l_size, l_mtime_ns FROM snapshot "
                "WHERE path = ? OR substr(path, 1, ?) = ?", (old, len(old) + 1, old + "/"),
            ).fetchall()
            self._conn.execute("BEGIN")
            try:
                for r in rows:
                    self._conn.execute("DELETE FROM snapshot WHERE path = ?", (r[0],))
                for r in rows:
                    self._conn.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?)",
                                       (new + r[0][len(old):], *r[1:]))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def snapshot_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]

    # --- runs -----------------------------------------------------------------

    def start_run(self, dry_run: bool) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)", (_now(), int(dry_run))
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, message: str, counts: dict, errors: list[str],
                   bytes_down: int = 0, bytes_up: int = 0, duration_s: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at=?, status=?, message=?, counts=?, errors=?, "
                "bytes_down=?, bytes_up=?, duration_s=? WHERE id=?",
                (_now(), status, message, json.dumps(counts), json.dumps(errors[:200]),
                 bytes_down, bytes_up, duration_s, run_id),
            )
            # Solo se guarda el historial reciente
            self._conn.execute("DELETE FROM runs WHERE id <= ?", (run_id - 500,))

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, status, dry_run, message, counts, errors, "
                "bytes_down, bytes_up, duration_s FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0], "started_at": r[1], "finished_at": r[2], "status": r[3],
                "dry_run": bool(r[4]), "message": r[5],
                "counts": json.loads(r[6] or "{}"), "errors": json.loads(r[7] or "[]"),
                "bytes_down": r[8], "bytes_up": r[9], "duration_s": r[10],
            }
            for r in rows
        ]

    # --- conflicts ------------------------------------------------------------

    def add_conflict(self, path: str, copy: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conflicts (path, conflict_copy, detected_at) VALUES (?,?,?)",
                (path, copy, _now()),
            )

    def conflicts(self, include_resolved: bool = False) -> list[dict]:
        sql = "SELECT id, path, conflict_copy, detected_at, resolved FROM conflicts"
        if not include_resolved:
            sql += " WHERE resolved = 0"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC").fetchall()
        return [
            {"id": r[0], "path": r[1], "conflict_copy": r[2], "detected_at": r[3], "resolved": bool(r[4])}
            for r in rows
        ]

    def resolve_conflict(self, conflict_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE conflicts SET resolved = 1 WHERE id = ?", (conflict_id,))
            return cur.rowcount > 0

    # --- borrados masivos pendientes de confirmar ---------------------------------------------
    # Un solo lote abierto (decision='pending'). Confirmar/descartar cambia la decisión; el run
    # siguiente la ejecuta y borra solo las filas que ha usado (una decisión tomada durante un run
    # en marcha no se pierde).

    def set_pending_batch(self, items: list[tuple[str, str, int, str]]) -> str:
        """Sustituye el lote pendiente por items [(kind, path, size, side)]. Devuelve su id, que se
        mantiene si el contenido no cambia (así los botones del panel no caducan entre runs)."""
        import uuid
        with self._lock:
            current = self._conn.execute(
                "SELECT kind, path, batch_id FROM pending_deletes WHERE decision = 'pending'"
            ).fetchall()
        if current and {(r[0], r[1]) for r in current} == {(k, p) for k, p, _, _ in items}:
            return current[0][2]
        batch_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM pending_deletes WHERE decision = 'pending'")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO pending_deletes (kind, path, batch_id, size, side, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    [(k, p, batch_id, sz, sd, _now()) for k, p, sz, sd in items],
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return batch_id

    def clear_pending_batch(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_deletes WHERE decision = 'pending'")

    def pending_items(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, path, batch_id, size, side, created_at FROM pending_deletes "
                "WHERE decision = 'pending' ORDER BY path"
            ).fetchall()
        return [{"kind": r[0], "path": r[1], "batch_id": r[2], "size": r[3], "side": r[4], "created_at": r[5]}
                for r in rows]

    def pending_info(self) -> tuple[int, str | None]:
        """(nº de elementos, id del lote) del lote pendiente; barato para consultarlo a menudo."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), MAX(batch_id) FROM pending_deletes WHERE decision = 'pending'"
            ).fetchone()
        return row[0], row[1]

    def pending_keys(self) -> set[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, path FROM pending_deletes WHERE decision = 'pending'").fetchall()
        return {(r[0], r[1]) for r in rows}

    def decide_paths(self, batch_id: str, keys: list[tuple[str, str]], decision: str) -> int:
        """Decide solo algunos elementos del lote (p.ej. una subcarpeta)."""
        assert decision in ("confirmed", "rejected")
        with self._lock:
            n = 0
            for kind, path in keys:
                cur = self._conn.execute(
                    "UPDATE pending_deletes SET decision = ? WHERE batch_id = ? AND kind = ? AND path = ? "
                    "AND decision = 'pending'", (decision, batch_id, kind, path),
                )
                n += cur.rowcount
            return n

    def decide_batch(self, batch_id: str, decision: str) -> int:
        assert decision in ("confirmed", "rejected")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_deletes SET decision = ? WHERE batch_id = ? AND decision = 'pending'",
                (decision, batch_id),
            )
            return cur.rowcount

    def decisions(self) -> dict[tuple[str, str], str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, path, decision FROM pending_deletes WHERE decision != 'pending'"
            ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def clear_decisions(self, keys: dict[tuple[str, str], str]) -> None:
        """Borra las decisiones leídas al empezar el run (solo si siguen igual)."""
        with self._lock:
            self._conn.executemany(
                "DELETE FROM pending_deletes WHERE kind = ? AND path = ? AND decision = ?",
                [(k, p, d) for (k, p), d in keys.items()],
            )

    # --- kv -------------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
