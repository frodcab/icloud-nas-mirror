# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Estado de la sync de Fotos (state/photos.db): qué fichero del NAS es qué foto de iCloud.

Hereda de StateDB el historial de runs, el lote de borrados pendientes de confirmar y el kv; el
snapshot de Drive no se usa aquí.
"""

from dataclasses import dataclass
from pathlib import Path

from services.state_db import StateDB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_files (
    path TEXT PRIMARY KEY,              -- ruta relativa a la carpeta de Fotos, p.ej. 2026/10/IMG_1.HEIC
    asset_id TEXT NOT NULL,
    role TEXT NOT NULL,                 -- original | live | edited | edited_live
    size INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    l_size INTEGER NOT NULL DEFAULT 0,
    l_mtime_ns INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0 -- borrado a propósito en el NAS: no se vuelve a bajar
);
CREATE INDEX IF NOT EXISTS photo_files_asset ON photo_files (asset_id);
CREATE TABLE IF NOT EXISTS photo_uploads (
    asset_id TEXT PRIMARY KEY,          -- foto subida desde Subir/ que aún no se ha colocado
    path TEXT NOT NULL                  -- dónde espera (Subir/.subidos/...)
);
CREATE TABLE IF NOT EXISTS album_links (
    path TEXT PRIMARY KEY,              -- enlace duro creado por la app, p.ej. Álbumes/Viaje/IMG_1.HEIC
    source TEXT NOT NULL                -- fichero al que apunta (AAAA/MM/...)
);
CREATE TABLE IF NOT EXISTS download_fails (
    asset_id TEXT NOT NULL,
    role TEXT NOT NULL,
    fails INTEGER NOT NULL,             -- intentos seguidos que han fallado (o se han pasado de tiempo)
    last_try REAL NOT NULL,
    error TEXT,
    PRIMARY KEY (asset_id, role)
);
CREATE TABLE IF NOT EXISTS upload_failures (
    path TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    error TEXT
);
"""


@dataclass
class PhotoFile:
    path: str
    asset_id: str
    role: str
    size: int = 0
    fingerprint: str | None = None
    l_size: int = 0
    l_mtime_ns: int = 0
    dismissed: bool = False


class PhotosDB(StateDB):
    def __init__(self, path: Path | str):
        super().__init__(path)
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # --- ficheros ---------------------------------------------------------------------------------

    def files(self) -> dict[str, PhotoFile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, asset_id, role, size, fingerprint, l_size, l_mtime_ns, dismissed FROM photo_files"
            ).fetchall()
        return {r[0]: PhotoFile(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7])) for r in rows}

    def put_file(self, f: PhotoFile) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO photo_files VALUES (?,?,?,?,?,?,?,?)",
                               (f.path, f.asset_id, f.role, f.size, f.fingerprint, f.l_size, f.l_mtime_ns,
                                int(f.dismissed)))

    def move_file(self, old: str, new: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE photo_files SET path = ? WHERE path = ?", (new, old))

    def forget_file(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM photo_files WHERE path = ?", (path,))

    def forget_asset(self, asset_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM photo_files WHERE asset_id = ?", (asset_id,))

    def asset_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(DISTINCT asset_id) FROM photo_files").fetchone()[0]

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, COUNT(*), COALESCE(SUM(size), 0) FROM photo_files WHERE dismissed = 0 GROUP BY role"
            ).fetchall()
            assets = self._conn.execute("SELECT COUNT(DISTINCT asset_id) FROM photo_files").fetchone()[0]
        return {"fotos": assets, "por_tipo": {r[0]: {"ficheros": r[1], "bytes": r[2]} for r in rows},
                "bytes": sum(r[2] for r in rows)}

    # --- subidas ----------------------------------------------------------------------------------

    def uploads(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute("SELECT asset_id, path FROM photo_uploads").fetchall())

    def add_upload(self, asset_id: str, path: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO photo_uploads VALUES (?, ?)", (asset_id, path))

    def done_upload(self, asset_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM photo_uploads WHERE asset_id = ?", (asset_id,))

    def upload_failed(self, path: str, error: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO upload_failures VALUES (?, 1, ?) ON CONFLICT(path) DO UPDATE SET "
                "count = count + 1, error = excluded.error", (path, error[:300]))
            return self._conn.execute("SELECT count FROM upload_failures WHERE path = ?", (path,)).fetchone()[0]

    def clear_upload_failure(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM upload_failures WHERE path = ?", (path,))

    # --- descargas que fallan ---------------------------------------------------------------------

    def download_fails(self) -> dict[tuple[str, str], tuple[int, float]]:
        with self._lock:
            return {(a, r): (n, t) for a, r, n, t in
                    self._conn.execute("SELECT asset_id, role, fails, last_try FROM download_fails")}

    def download_failed(self, asset_id: str, role: str, error: str, now: float) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO download_fails VALUES (?, ?, 1, ?, ?) ON CONFLICT(asset_id, role) DO UPDATE SET "
                "fails = fails + 1, last_try = excluded.last_try, error = excluded.error",
                (asset_id, role, now, error[:300]))
            return self._conn.execute("SELECT fails FROM download_fails WHERE asset_id = ? AND role = ?",
                                      (asset_id, role)).fetchone()[0]

    def download_ok(self, asset_id: str, role: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM download_fails WHERE asset_id = ? AND role = ?", (asset_id, role))

    # --- enlaces de álbumes -----------------------------------------------------------------------

    def album_links(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute("SELECT path, source FROM album_links").fetchall())

    def add_album_link(self, path: str, source: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO album_links VALUES (?, ?)", (path, source))

    def drop_album_link(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM album_links WHERE path = ?", (path,))

