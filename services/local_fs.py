# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Lado local: escaneo, escritura atómica y papelera (nunca se borra nada con rm)."""

import logging
import os
import shutil
import time
import unicodedata
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path

from services.entries import LocalEntry

log = logging.getLogger(__name__)

TMP_PREFIX = ".icloud-sync-tmp-"


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def is_excluded(rel_path: str, excludes: list[str]) -> bool:
    for part in rel_path.split("/"):
        if part.startswith(TMP_PREFIX):
            return True
        if any(fnmatch(part, pattern) for pattern in excludes):
            return True
    return False


def in_scope(rel_path: str, folders: list[str]) -> bool:
    """Dentro de SYNC_FOLDERS, o antepasado de una de ellas (hay que recorrerlo para llegar)."""
    if not folders:
        return True
    return any(
        rel_path == f or rel_path.startswith(f + "/") or f.startswith(rel_path + "/")
        for f in folders
    )


def is_scope_ancestor(rel_path: str, folders: list[str]) -> bool:
    """Carpetas estructurales (antepasados de SYNC_FOLDERS o ellas mismas): nunca se borran."""
    return any(rel_path == f or f.startswith(rel_path + "/") for f in folders)


class LocalTree:
    def __init__(self, root: Path, trash_dir: Path, trash_prefix: str = ""):
        self.root = root
        self.trash_dir = trash_dir
        self.trash_prefix = trash_prefix   # p.ej. "Apps": trash/AAAA-MM-DD/Apps/...

    def abs(self, real_rel: str) -> Path:
        return self.root / real_rel

    def scan(self, excludes: list[str], folders: list[str]) -> tuple[dict[str, LocalEntry], list[str]]:
        """Devuelve {ruta NFC: entrada} y la lista de avisos.

        Lo que no se puede leer o representar se deja en self.ignored: el motor lo aparta del plan
        en los DOS lados. Si solo desapareciera de aquí, parecería "borrado en el NAS" y se borraría
        en iCloud (pasó con dos nombres que solo se distinguían en mayúsculas).
        """
        entries: dict[str, LocalEntry] = {}
        warnings: list[str] = []
        self.ignored: set[str] = set()

        def walk(real_dir: str) -> None:
            try:
                with os.scandir(self.root / real_dir) as it:
                    items = sorted(it, key=lambda e: e.name)
            except OSError as exc:
                warnings.append(f"No se puede leer {real_dir or '/'}: {exc}")
                self.ignored.add(nfc(real_dir))
                return
            for item in items:
                real = f"{real_dir}/{item.name}" if real_dir else item.name
                if item.name.startswith(TMP_PREFIX) and item.is_file(follow_symlinks=False):
                    # Descarga a medias de un run cortado (reinicio): es nuestra y no vale para nada
                    try:
                        os.unlink(item.path)
                    except OSError:
                        pass
                    continue
                key = nfc(real)
                if is_excluded(key, excludes) or not in_scope(key, folders):
                    continue
                if item.is_symlink():
                    warnings.append(f"Enlace simbólico ignorado: {key}")
                    self.ignored.add(key)
                    continue
                if key in entries:
                    # Mismo nombre en NFC y NFD en disco: no se puede saber cuál es "el" fichero
                    warnings.append(f"Ignorado '{key}': hay dos nombres iguales con distinta codificación Unicode")
                    self.ignored.add(key)
                    continue
                if item.is_dir():
                    entries[key] = LocalEntry(key, real, True)
                    walk(real)
                elif item.is_file():
                    st = item.stat()
                    entries[key] = LocalEntry(key, real, False, st.st_size, st.st_mtime_ns)

        walk("")
        return entries, warnings

    def stat(self, real_rel: str) -> LocalEntry:
        st = self.abs(real_rel).stat()
        return LocalEntry(nfc(real_rel), real_rel, False, st.st_size, st.st_mtime_ns)

    def tmp_path_for(self, real_rel: str) -> Path:
        target = self.abs(real_rel)
        return target.with_name(f"{TMP_PREFIX}{target.name}")

    def commit_download(self, tmp: Path, real_rel: str, mtime: float) -> LocalEntry:
        """El fichero ya descargado en tmp pasa a su sitio de forma atómica, con la fecha de iCloud."""
        if mtime:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, self.abs(real_rel))
        return self.stat(real_rel)

    def mkdir(self, real_rel: str) -> None:
        self.abs(real_rel).mkdir(parents=True, exist_ok=True)

    def rename(self, real_rel: str, new_real_rel: str) -> None:
        os.rename(self.abs(real_rel), self.abs(new_real_rel))

    def exists(self, real_rel: str) -> bool:
        return self.abs(real_rel).exists()

    def to_trash(self, real_rel: str) -> Path:
        """Mueve a data/trash/AAAA-MM-DD/<ruta> en vez de borrar."""
        dest = self.trash_dir / datetime.now().strftime("%Y-%m-%d") / self.trash_prefix / real_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.name}.{int(time.time())}")
        shutil.move(str(self.abs(real_rel)), str(dest))
        return dest

    def rmdir_if_empty(self, real_rel: str) -> bool:
        try:
            self.abs(real_rel).rmdir()
            return True
        except OSError:
            return False

    def purge_trash(self, days: int) -> int:
        if not self.trash_dir.exists():
            return 0
        limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        removed = 0
        for day in self.trash_dir.iterdir():
            if day.is_dir() and day.name < limit:
                shutil.rmtree(day, ignore_errors=True)
                removed += 1
        return removed
