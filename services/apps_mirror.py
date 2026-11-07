# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Copia de las App Libraries (Pages, Numbers, Keynote, Atajos...) SOLO de iCloud al NAS.

A diferencia de la sync principal, aquí iCloud siempre manda: lo que cambie o se borre en la app se
refleja en el NAS, y si en el NAS se toca algo, se vuelve a dejar como en iCloud. Nada se borra sin
más: lo que se sustituye o retira en el NAS va a la papelera local (data/trash). En iCloud nunca se
escribe nada.
"""

import logging
import posixpath

from services.entries import Action, AuthRequired, LocalEntry, RemoteEntry, RunResult, SnapshotEntry, SyncAborted
from services.local_fs import LocalTree
from services.packages import flatten_package_zip
from services.progress import Progress
from services.state_db import StateDB

log = logging.getLogger(__name__)

MTIME_TOLERANCE_S = 2.0
TRASH_KINDS = {"apps_replace", "apps_trash", "apps_rmdir"}


def _top(path: str) -> str:
    return path.split("/", 1)[0]


def _depth(path: str) -> int:
    return path.count("/")


def plan_mirror(local: dict[str, LocalEntry], remote: dict[str, RemoteEntry],
                snapshot: dict[str, SnapshotEntry]) -> tuple[list[Action], list[str]]:
    actions: list[Action] = []
    warnings: list[str] = []

    # Una app que iCloud devuelve vacía pero que en el NAS tiene ficheros: casi siempre es una
    # respuesta incompleta de Apple, no que se haya vaciado. No se retira nada de esa app.
    remote_files_by_app: dict[str, int] = {}
    for p, e in remote.items():
        if not e.is_dir and "/" in p:
            remote_files_by_app[_top(p)] = remote_files_by_app.get(_top(p), 0) + 1
    frozen = {
        app for app in {_top(p) for p, e in local.items() if not e.is_dir and "/" in p}
        if app in remote and not remote_files_by_app.get(app)
    }
    for app in sorted(frozen):
        warnings.append(f"App '{app}': iCloud la devuelve vacía; no se retira nada del NAS por si acaso")

    for p in sorted(set(remote) | set(local) | set(snapshot)):
        r, l, s = remote.get(p), local.get(p), snapshot.get(p)
        if r is not None and l is not None and r.is_dir != l.is_dir:
            warnings.append(f"Ignorado '{p}': es carpeta en un lado y fichero en el otro")
            continue
        if (r and r.is_dir) or (l and l.is_dir) or (s and s.is_dir and not r and not l):
            continue   # las carpetas van después
        if r is not None:
            if l is None:
                actions.append(Action("apps_download", p, "nuevo en la app"))
            elif s is None:
                same = l.size == r.size and abs(l.mtime_ns / 1e9 - r.mtime) <= MTIME_TOLERANCE_S
                actions.append(Action("adopt", p, "ya estaba igual") if same
                               else Action("apps_replace", p, "distinto de iCloud"))
            elif r.etag != s.etag:
                actions.append(Action("apps_replace", p, "cambiado en la app"))
            elif l.size != s.l_size or l.mtime_ns != s.l_mtime_ns:
                actions.append(Action("apps_replace", p, "tocado en el NAS: manda iCloud"))
        elif l is not None:
            if _top(p) not in frozen:
                actions.append(Action("apps_trash", p, "ya no está en la app"))
        elif s is not None:
            actions.append(Action("forget", p, "ya no está en ningún lado"))

    # Carpetas: se crean las de iCloud que falten; las del NAS que ya no existan en iCloud se retiran
    # de la más profunda a la menos (salvo que tengan dentro algo que se conserva).
    keep = {a for p, e in remote.items() for a in [p, *_ancestors(p)]}
    keep |= {a for p in local for a in _ancestors(p) if _top(p) in frozen}
    mkdirs = [Action("apps_mkdir", p, "carpeta nueva en la app")
              for p in sorted(remote, key=lambda x: (_depth(x), x)) if remote[p].is_dir and p not in local]
    actions[:0] = mkdirs   # antes que las descargas, de menos a más profunda
    for p in sorted((p for p, e in local.items() if e.is_dir and p not in keep), key=lambda x: -_depth(x)):
        if _top(p) not in frozen:
            actions.append(Action("apps_rmdir", p, "carpeta que ya no está en la app"))
    for p, s in snapshot.items():
        if s.is_dir and p not in remote and p not in local:
            actions.append(Action("forget", p, "carpeta que ya no está en ningún lado"))
    return actions, warnings


def _ancestors(path: str) -> list[str]:
    out, parent = [], posixpath.dirname(path)
    while parent:
        out.append(parent)
        parent = posixpath.dirname(parent)
    return out


class AppsMirror:
    def __init__(self, db: StateDB, local: LocalTree, remote, excludes: list[str], max_delete: int,
                 progress: Progress | None = None):
        self.db, self.local, self.remote = db, local, remote
        self.excludes, self.max_delete, self.progress = excludes, max_delete, progress

    def run(self, dry_run: bool = False) -> RunResult:
        """Se lanza después de la sync principal, que es la que descubre las App Libraries."""
        result = RunResult(dry_run=dry_run)
        if not getattr(self.remote, "app_libraries", None):
            return result
        if self.progress:
            self.progress.set_phase("apps")
        remote, rw = self.remote.list_apps(self.excludes)
        self.local.root.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            self._migrate_packages()
        local, lw = self.local.scan(self.excludes, [])
        snapshot = self.db.load_snapshot()
        result.warnings.extend(rw + lw)

        ignored = getattr(self.local, "ignored", set()) | getattr(self.remote, "apps_ignored", set())
        if ignored:
            def visible(p):
                return not any(p == i or p.startswith(i + "/") for i in ignored)
            local, remote, snapshot = ({p: e for p, e in d.items() if visible(p)} for d in (local, remote, snapshot))

        actions, pw = plan_mirror(local, remote, snapshot)
        result.warnings.extend(pw)
        trashed = [a for a in actions if a.kind in TRASH_KINDS - {"apps_replace"}]
        if len(trashed) > self.max_delete:
            raise SyncAborted(f"La copia de las apps retiraría {len(trashed)} elementos del NAS "
                              f"(límite MAX_DELETE={self.max_delete}). No se toca nada.")

        visible_actions = [a for a in actions if a.kind not in ("adopt", "forget")]
        for a in actions:
            result.counts[a.kind] = result.counts.get(a.kind, 0) + 1
        if self.progress:
            self.progress.set_plan(len(visible_actions),
                                   sum(remote[a.path].size for a in actions if a.kind in ("apps_download", "apps_replace")))
        if dry_run:
            for a in actions:
                log.info("[dry-run] %s Apps/%s (%s)", a.kind, a.path, a.reason)
            return result

        result.counts = {}
        self._local, self._remote = local, remote
        for a in actions:
            if self.progress and self.progress.stop_requested:
                result.status = "stopped"
                return result
            silent = a.kind in ("adopt", "forget")
            size = remote[a.path].size if a.kind in ("apps_download", "apps_replace") else 0
            shown = f"Apps/{a.path}"
            if self.progress and not silent:
                self.progress.action_start(a.kind, shown, size)
            try:
                getattr(self, f"_do_{a.kind}")(a.path)
                log.info("%s %s (%s)", a.kind, shown, a.reason)
                result.counts[a.kind] = result.counts.get(a.kind, 0) + 1
                if self.progress and not silent:
                    self.progress.action_done(a.kind, shown, size, True, reason=a.reason)
            except AuthRequired:
                raise
            except Exception as exc:
                msg = f"{a.kind} {shown}: {type(exc).__name__}: {exc}"
                log.warning(msg)
                result.errors.append(msg)
                if self.progress and not silent:
                    self.progress.action_done(a.kind, shown, size, False, error=f"{type(exc).__name__}: {exc}")
        if result.errors:
            result.status = "partial"
        return result

    def _migrate_packages(self) -> None:
        """Una sola vez: los paquetes copiados antes de guardarlos como archivo único llevan la
        carpeta dentro del zip. Se arreglan en el NAS (sin volver a descargarlos) y se actualiza el
        snapshot para que no parezcan "tocados en el NAS"."""
        if self.db.get("package_format") == "flat":
            return
        fixed = 0
        for path, snap in self.db.load_snapshot().items():
            if snap.is_dir or snap.r_size == snap.l_size:
                continue                     # los paquetes son los que llegan con otro tamaño
            f = self.local.root / path
            try:
                if f.is_file() and flatten_package_zip(f, posixpath.basename(path)):
                    st = f.stat()
                    snap.l_size, snap.l_mtime_ns = st.st_size, st.st_mtime_ns
                    self.db.put(snap)
                    fixed += 1
            except (OSError, ValueError) as exc:
                log.warning("No se pudo convertir %s a archivo único: %s", path, exc)
        if fixed:
            log.info("Apps: %d documentos de Pages/Numbers/Keynote pasados a archivo único", fixed)
        self.db.set("package_format", "flat")

    # --- acciones -------------------------------------------------------------------------------

    def _real(self, path: str) -> str:
        e = self._local.get(path)
        if e is not None:
            return e.real
        parent = posixpath.dirname(path)
        return posixpath.join(self._real(parent), posixpath.basename(path)) if parent else posixpath.basename(path)

    def _do_apps_mkdir(self, path: str) -> None:
        real = self._real(path)
        self.local.mkdir(real)
        self._local[path] = LocalEntry(path, real, True)
        self.db.put(SnapshotEntry(path, True))

    def _do_apps_download(self, path: str) -> None:
        r = self._remote[path]
        real = self._real(path)
        tmp = self.local.tmp_path_for(real)
        try:
            self.remote.download(r, tmp, nodes=self.remote.app_nodes)
            l = self.local.commit_download(tmp, real, r.mtime)
        finally:
            tmp.unlink(missing_ok=True)
        self._local[path] = LocalEntry(path, real, False, l.size, l.mtime_ns)
        self.db.put(SnapshotEntry(path, False, r.etag, r.size, r.mtime, l.size, l.mtime_ns))

    def _do_apps_replace(self, path: str) -> None:
        # La versión del NAS a la papelera antes de traer la de iCloud
        self.local.to_trash(self._local.pop(path).real)
        self._do_apps_download(path)

    def _do_adopt(self, path: str) -> None:
        l, r = self._local[path], self._remote[path]
        self.db.put(SnapshotEntry(path, False, r.etag, r.size, r.mtime, l.size, l.mtime_ns))

    def _do_apps_trash(self, path: str) -> None:
        self.local.to_trash(self._local.pop(path).real)
        self.db.forget(path)

    def _do_apps_rmdir(self, path: str) -> None:
        real = self._local.pop(path).real
        if not self.local.rmdir_if_empty(real):
            self.local.to_trash(real)
        self.db.forget(path)

    def _do_forget(self, path: str) -> None:
        self.db.forget(path)
