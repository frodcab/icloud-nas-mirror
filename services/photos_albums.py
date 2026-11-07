# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Álbumes de iCloud Fotos en el NAS: Photos/iCloud/Álbumes/<álbum>/ (solo iCloud → NAS).

- Cada álbum (los tuyos, con sus carpetas, y Favoritas) es una carpeta de ENLACES DUROS a las fotos de
  AAAA/MM: la misma foto, sin ocupar más. Se mantienen iguales a iCloud: lo que sale del álbum, se quita
  de la carpeta (solo el enlace; la foto sigue en su mes). Lo que se cambie en esas carpetas no sube.
- Oculto es distinto: ahí las fotos viven DE VERDAD (no están en AAAA/MM ni en otros álbumes). Ocultar
  una foto en el iPhone la mueve a Álbumes/Oculto/; mostrarla, la devuelve a su mes. Borrarla en el NAS
  la borra en iCloud, como cualquier otra.
- Refresco con Apple una vez al día o cuando la lista de cambios avisa de álbumes; el reenlazado
  (sin peticiones) en cada vuelta, para que lo recién bajado entre en sus álbumes.
"""

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime

from services.photos_remote import RemoteAsset

log = logging.getLogger(__name__)

ALBUMS_DIR = "Álbumes"
HIDDEN_DIR = f"{ALBUMS_DIR}/Oculto"   # ocultas, si no se configura otra carpeta (PHOTOS_HIDDEN_DIR)
EXTERNAL_HIDDEN = "@oculto"            # prefijo en photos.db de la carpeta de ocultas fuera de Fotos
ALBUMS_EVERY_S = 24 * 3600        # leer todos los álbumes cuesta ~20 min (solo Oculto, ~28.000 fotos)
_BAD = re.compile(r'[\x00-\x1f\\:*?"<>|]+')


def _clean(name: str) -> str:
    name = _BAD.sub("_", name).strip().strip(".")
    return name or "_"


class AlbumsMirror:
    def __init__(self, engine):
        self.e = engine

    # --- estado ---------------------------------------------------------------------------------

    @property
    def hidden_ids(self) -> set[str]:
        return set(json.loads(self.e.db.get("hidden_ids") or "[]"))

    def _save_hidden(self, ids: set[str]) -> None:
        if not self.e._dry:
            self.e.db.set("hidden_ids", json.dumps(sorted(ids)) if ids else None)

    def due(self) -> bool:
        return (self.e.db.get("albums_dirty") == "1"
                or time.time() - float(self.e.db.get("albums_at") or 0) > ALBUMS_EVERY_S)

    # --- refresco con Apple ---------------------------------------------------------------------

    def refresh(self, local, uploads, budget) -> None:
        e = self.e
        e.progress.set_phase("listing")
        memberships: dict[str, list[str]] = {}
        hidden_assets: list[RemoteAsset] = []
        for kind, path, album in e.remote.albums():
            assets = e.remote.album_assets(album)
            if kind == "hidden":
                hidden_assets = assets
                continue
            folder = "/".join(_clean(p) for p in path) or "Sin nombre"
            memberships.setdefault(f"{ALBUMS_DIR}/{folder}", []).extend(a.id for a in assets)
        log.info("Fotos: %d álbumes y %d fotos ocultas en iCloud", len(memberships), len(hidden_assets))

        # Oculto: lo que ya está en el NAS se mueve; lo que falta lo baja cada vuelta (engine._hidden_downloads,
        # pidiéndolas por id: aquí las URL ya habrían caducado). Con PHOTOS_HIDDEN desactivado solo se apuntan
        # (para no tomarlas por borradas): ni se bajan ni se mueven
        before = self.hidden_ids
        now_hidden = {a.id for a in hidden_assets}
        known = e.db.files()
        for aid in (now_hidden if e.hidden_enabled else ()):
            self.hide(aid, known)
        for aid in before - now_hidden:
            self.unhide(aid, e.db.files())
        self._save_hidden(now_hidden)
        if not e._dry:
            e.db.set("albums_json", json.dumps(memberships))
            e.db.set("albums_at", str(time.time()))
            e.db.set("albums_dirty", None)

    # --- Oculto ---------------------------------------------------------------------------------

    def hide(self, aid: str, known) -> None:
        """Mueve a Álbumes/Oculto/ los ficheros de una foto que se ha ocultado en iCloud."""
        for pf in [f for f in known.values() if f.asset_id == aid and not f.dismissed]:
            if self.e.is_hidden_path(pf.path):
                continue
            self._move(pf, self.e.hidden_prefix, "ocultada en iCloud")

    def unhide(self, aid: str, known) -> None:
        """La foto ya no está oculta: vuelve a su AAAA/MM (la fecha del fichero es la de la foto)."""
        for pf in [f for f in known.values() if f.asset_id == aid and not f.dismissed]:
            if not (self.e.is_hidden_path(pf.path) or pf.path.startswith(HIDDEN_DIR + "/")):
                continue
            try:
                mtime = self.e.abs(pf.path).stat().st_mtime
            except OSError:
                continue
            self._move(pf, datetime.fromtimestamp(mtime).strftime("%Y/%m"), "ya no está oculta")

    def migrate_hidden(self, old_prefix: str, known) -> int:
        """Si cambia la carpeta de ocultas (PHOTOS_HIDDEN_DIR), se llevan ahí las que había en la de antes:
        si no, al no verlas parecerían borradas en el NAS (y se borrarían en iCloud)."""
        n = 0
        for pf in [f for f in known.values() if f.path.startswith(old_prefix + "/")]:
            self._move(pf, self.e.hidden_prefix, "pasa a la carpeta de ocultas configurada")
            n += 1
        return n

    def _move(self, pf, folder: str, why: str) -> None:
        e = self.e
        name = os.path.basename(pf.path)
        dest = f"{folder}/{name}"
        stem, ext = os.path.splitext(name)
        n = 1
        while e.abs(dest).exists() or dest in e.db.files():
            n += 1
            dest = f"{folder}/{stem} ({n}){ext}"
        log.info("Fotos: %s %s → %s", pf.path, why, dest)
        e._count("ocultas" if folder == e.hidden_prefix else "mostradas")
        if e._dry:
            return
        if e.abs(pf.path).exists():
            e.abs(dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(e.abs(pf.path)), str(e.abs(dest)))
        e.db.move_file(pf.path, dest)
        self.drop_links_to(pf.path)
        pf.path = dest

    # --- enlaces --------------------------------------------------------------------------------

    def drop_links_to(self, source: str) -> None:
        for link, src in self.e.db.album_links().items():
            if src == source:
                self._unlink(link)

    def relink(self) -> None:
        """Deja cada carpeta de álbum con un enlace por fichero de sus fotos ya bajadas (sin pedir nada
        a Apple). Las fotos ocultas no se enlazan en otros álbumes: seguirían a la vista."""
        e = self.e
        if e._dry:
            return
        memberships: dict[str, list[str]] = json.loads(e.db.get("albums_json") or "{}")
        if f"{ALBUMS_DIR}/" in memberships:      # guardado antes del arreglo del álbum sin nombre
            memberships[f"{ALBUMS_DIR}/Sin nombre"] = memberships.pop(f"{ALBUMS_DIR}/")
        by_asset: dict[str, list] = {}
        for pf in e.db.files().values():
            if not pf.dismissed and not self.e.is_hidden_path(pf.path) and not pf.path.startswith(HIDDEN_DIR + "/"):
                by_asset.setdefault(pf.asset_id, []).append(pf)
        hidden = self.hidden_ids
        desired: dict[str, str] = {}
        for folder, ids in memberships.items():
            taken: set[str] = set()
            for aid in ids:
                if aid in hidden:
                    continue
                for pf in sorted(by_asset.get(aid, []), key=lambda f: f.path):
                    name = os.path.basename(pf.path)
                    stem, ext = os.path.splitext(name)
                    n = 1
                    while name.lower() in taken:
                        n += 1
                        name = f"{stem} ({n}){ext}"
                    taken.add(name.lower())
                    desired[f"{folder}/{name}"] = pf.path
        current = e.db.album_links()
        made = removed = 0
        for link, src in current.items():
            if desired.get(link) != src:
                self._unlink(link)
                removed += 1
        for link, src in desired.items():
            if current.get(link) == src and (e.root / link).exists():
                continue
            if self._link(src, link):
                made += 1
        self._prune_empty_dirs()
        if made or removed:
            log.info("Fotos: álbumes al día (%d enlaces nuevos, %d quitados)", made, removed)
            e._count("enlaces_album", made)

    def _link(self, src: str, link: str) -> bool:
        e = self.e
        target, path = e.abs(src), e.root / link
        if not target.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists():
                if os.path.samefile(path, target):
                    e.db.add_album_link(link, src)
                    return False
                if link not in e.db.album_links():
                    log.warning("Fotos: %s ya existe y no es un enlace de la app; se deja", link)
                    return False
                path.unlink()
            os.link(target, path)
        except OSError as exc:
            log.warning("Fotos: no se pudo enlazar %s → %s (%s)", link, src, exc)
            return False
        e.db.add_album_link(link, src)
        return True

    def _unlink(self, link: str) -> None:
        e = self.e
        try:
            (e.root / link).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Fotos: no se pudo quitar el enlace %s (%s)", link, exc)
            return
        e.db.drop_album_link(link)

    def _prune_empty_dirs(self) -> None:
        """Quita las carpetas de álbum que se han quedado vacías (solo rmdir: nunca borra ficheros)."""
        base = self.e.root / ALBUMS_DIR
        if not base.is_dir():
            return
        for dirpath, _dirs, _files in sorted(os.walk(base), key=lambda t: -len(t[0])):
            if os.path.relpath(dirpath, self.e.root) in (ALBUMS_DIR, HIDDEN_DIR):
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass          # no está vacía
