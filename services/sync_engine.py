# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Reconciliación a tres bandas: árbol local, árbol de iCloud y snapshot de la última sync.

Regla de oro: ante la duda, no se pierde nada. Un borrado solo se propaga si el otro lado no ha
cambiado desde la última sync; un cambio en los dos lados conserva las dos versiones; lo que se
borra en local va a la papelera de data/trash y lo que se borra en iCloud a "Eliminados recientemente".
"""

import json
import logging
import os
import posixpath
from datetime import datetime
from pathlib import Path
from typing import Protocol

from core import names
from services.entries import (
    Action, AuthRequired, LocalEntry, RemoteEntry, RunResult, SnapshotEntry, SyncAborted,
)
from services.local_fs import LocalTree, is_scope_ancestor
from services.progress import Progress
from services.state_db import StateDB

log = logging.getLogger(__name__)

MTIME_TOLERANCE_S = 2.0
DELETE_KINDS = {"delete_local", "delete_remote", "rmdir_local", "rmdir_remote"}
# Descartar un borrado = hacer lo contrario: traerlo del lado donde sigue
INVERSE = {"delete_remote": "download", "delete_local": "upload",
           "rmdir_remote": "mkdir_local", "rmdir_local": "mkdir_remote"}
# En qué lado se borraría (lo que el usuario confirma o descarta)
DELETE_SIDE = {"delete_remote": "icloud", "rmdir_remote": "icloud", "delete_local": "nas", "rmdir_local": "nas"}
# Contabilidad interna del snapshot: no se enseña en el panel ni cuenta como operación
SILENT_KINDS = {"adopt", "forget"}


class Remote(Protocol):
    def list_tree(self, excludes: list[str], folders: list[str]) -> tuple[dict[str, RemoteEntry], list[str]]: ...
    def download(self, entry: RemoteEntry, dest: Path) -> None: ...
    def upload(self, source: Path, rel_path: str, mtime: float) -> RemoteEntry | None: ...
    def trash(self, entry: RemoteEntry) -> None: ...
    def mkdir(self, rel_path: str) -> RemoteEntry: ...


def _parent(path: str) -> str:
    return posixpath.dirname(path)


def _ancestors(path: str) -> list[str]:
    out = []
    parent = _parent(path)
    while parent:
        out.append(parent)
        parent = _parent(parent)
    return out


def _under_any(path: str, roots: set[str]) -> bool:
    if path in roots:
        return True
    return any(a in roots for a in _ancestors(path)) or ("" in roots)


def _depth(path: str) -> int:
    return path.count("/")


def plan(
    local: dict[str, LocalEntry],
    remote: dict[str, RemoteEntry],
    snapshot: dict[str, SnapshotEntry],
    folders: list[str],
) -> tuple[list[Action], list[str]]:
    """Función pura: decide qué hacer con cada ruta. Devuelve (acciones, avisos)."""
    actions: list[Action] = []
    warnings: list[str] = []

    clashes = {
        p for p in local.keys() & remote.keys() if local[p].is_dir != remote[p].is_dir
    }
    for p in sorted(clashes):
        warnings.append(f"Ignorado '{p}': es carpeta en un lado y fichero en el otro")

    def file_of(d: dict, p: str):
        e = d.get(p)
        return e if e is not None and not e.is_dir else None

    file_paths = {
        p for d in (local, remote, snapshot) for p, e in d.items() if not e.is_dir
    } - clashes
    survivors: set[str] = set(clashes)

    for p in sorted(file_paths):
        l, r, s = file_of(local, p), file_of(remote, p), file_of(snapshot, p)

        if s is None:
            if l and r:
                same = l.size == r.size and abs(l.mtime_ns / 1e9 - r.mtime) <= MTIME_TOLERANCE_S
                actions.append(Action("adopt" if same else "conflict", p, "nuevo en los dos lados"))
            elif l:
                actions.append(Action("upload", p, "nuevo en local"))
            elif r:
                actions.append(Action("download", p, "nuevo en iCloud"))
            if l or r:
                survivors.add(p)
            continue

        l_same = l is not None and l.size == s.l_size and l.mtime_ns == s.l_mtime_ns
        # etag None = se subió pero no se pudo leer el etag nuevo; vale el tamaño
        pending_etag = s.etag is None
        r_same = r is not None and (
            r.size == s.r_size if pending_etag else r.etag == s.etag
        )

        if l is None and r is None:
            actions.append(Action("forget", p, "borrado en los dos lados"))
            continue
        if l is None:
            if r_same:
                actions.append(Action("delete_remote", p, "borrado en local"))
            else:
                actions.append(Action("download", p, "borrado en local pero cambiado en iCloud"))
                survivors.add(p)
            continue
        if r is None:
            if l_same:
                actions.append(Action("delete_local", p, "borrado en iCloud"))
            else:
                actions.append(Action("upload", p, "borrado en iCloud pero cambiado en local"))
                survivors.add(p)
            continue

        survivors.add(p)
        if l_same and r_same:
            if pending_etag:
                actions.append(Action("adopt", p, "confirmar etag tras subida"))
        elif l_same:
            actions.append(Action("download", p, "cambiado en iCloud"))
        elif r_same:
            actions.append(Action("replace", p, "cambiado en local"))
        else:
            actions.append(Action("conflict", p, "cambiado en los dos lados"))

    # --- carpetas: de más profunda a menos, para saber si algo sobrevive debajo -----------------
    required = {a for p in survivors for a in _ancestors(p)}
    dir_paths = {
        p for d in (local, remote, snapshot) for p, e in d.items() if e.is_dir
    } - clashes
    dir_survives: dict[str, bool] = {}
    dir_actions: list[Action] = []
    for d in sorted(dir_paths, key=lambda x: (-_depth(x), x)):
        in_l = d in local and local[d].is_dir
        in_r = d in remote and remote[d].is_dir
        in_s = d in snapshot and snapshot[d].is_dir
        survives = (
            d in required
            or is_scope_ancestor(d, folders)
            or (in_l and in_r)
            or not in_s
            or any(v for c, v in dir_survives.items() if _parent(c) == d)
        )
        dir_survives[d] = survives
        if survives:
            if in_l and in_r:
                if not in_s:
                    dir_actions.append(Action("adopt", d, "carpeta en los dos lados"))
            elif in_l:
                dir_actions.append(Action("mkdir_remote", d, "carpeta nueva en local"))
            elif in_r:
                dir_actions.append(Action("mkdir_local", d, "carpeta nueva en iCloud"))
        elif in_l:
            dir_actions.append(Action("rmdir_local", d, "carpeta borrada en iCloud"))
        elif in_r:
            dir_actions.append(Action("rmdir_remote", d, "carpeta borrada en local"))
        else:
            dir_actions.append(Action("forget", d, "carpeta borrada en los dos lados"))

    return dir_actions + actions, warnings


def _unique_pairs(candidates: dict[str, list[str]]) -> list[tuple[str, str]]:
    used = [n for ns in candidates.values() for n in ns]
    return sorted((g, ns[0]) for g, ns in candidates.items() if len(ns) == 1 and used.count(ns[0]) == 1)


def _dir_moves(gone, new, gone_sig, new_sig, parent_ok) -> list[tuple[str, str]]:
    gone = {d for d in gone if not any(a in gone for a in _ancestors(d))}
    new = {d for d in new if not any(a in new for a in _ancestors(d))}
    cands: dict[str, list[str]] = {}
    for g in gone:
        gs = gone_sig(g)
        if not gs:
            continue
        for n in new:
            ns = new_sig(n)
            if gs <= ns and len(ns - gs) <= max(2, len(gs) // 20):
                cands.setdefault(g, []).append(n)
    return [(g, n) for g, n in _unique_pairs(cands) if parent_ok(_parent(n))]


def detect_moves(local, remote, snapshot) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Renombrados y movimientos, de carpetas y de ficheros, en los dos sentidos.
    Devuelve ([(dónde pasó: "remote"|"local", ruta vieja, ruta nueva)], rutas viejas de ficheros
    RENOMBRADOS en el NAS). Estos últimos no se renombran en iCloud (pyicloud manda solo "name" y en
    ficheros iCloud separa nombre y extensión: sin verificar), se suben con el nombre nuevo y el viejo
    se borra sin pedir confirmación, porque su contenido sigue ahí. Solo emparejamientos únicos
    en los dos sentidos; ante la duda, nada (se hace lo de siempre: borrado pendiente + nuevo).

    - Carpetas: mismo contenido (nombres relativos y tamaños; se admiten unos pocos nuevos).
    - Ficheros: mismo tamaño y fecha (un renombrado la conserva), el lado contrario intacto y
      nunca ficheros de 0 bytes."""
    def sig(entries, root, size_of):
        pre = root + "/"
        return {(p[len(pre):], size_of(e)) for p, e in entries.items() if not e.is_dir and p.startswith(pre)}

    def is_dir(d, p):
        return p in d and d[p].is_dir

    moves: list[tuple[str, str, str]] = []
    # carpetas movidas en iCloud → se aplica en el NAS
    gone = {p for p, e in snapshot.items() if e.is_dir and is_dir(local, p) and p not in remote}
    new = {p for p, e in remote.items() if e.is_dir and p not in snapshot and p not in local}
    for g, n in _dir_moves(gone, new, lambda g: sig(snapshot, g, lambda e: e.r_size),
                           lambda n: sig(remote, n, lambda e: e.size), lambda par: not par or is_dir(local, par)):
        moves.append(("remote", g, n))
    # carpetas movidas en el NAS → se aplica en iCloud
    gone = {p for p, e in snapshot.items() if e.is_dir and is_dir(remote, p) and p not in local}
    new = {p for p, e in local.items() if e.is_dir and p not in snapshot and p not in remote}
    for g, n in _dir_moves(gone, new, lambda g: sig(snapshot, g, lambda e: e.l_size),
                           lambda n: sig(local, n, lambda e: e.size), lambda par: not par or is_dir(remote, par)):
        moves.append(("local", g, n))

    moved_roots = [(side, g, n) for side, g, n in moves]

    def covered(p):
        return any(p == g or p.startswith(g + "/") or p == n or p.startswith(n + "/") for _, g, n in moved_roots)

    # ficheros movidos en el NAS: falta en el NAS, intacto en iCloud; nuevo en el NAS con mismo tamaño+fecha
    cands: dict[str, list[str]] = {}
    new_by_key: dict[tuple, list[str]] = {}
    for p, e in local.items():
        if not e.is_dir and e.size and p not in snapshot and p not in remote and not covered(p):
            new_by_key.setdefault((e.size, e.mtime_ns), []).append(p)
    for p, s in snapshot.items():
        r = remote.get(p)
        if (not s.is_dir and s.l_size and p not in local and r is not None and not r.is_dir
                and s.etag is not None and r.etag == s.etag and not covered(p)):
            for n in new_by_key.get((s.l_size, s.l_mtime_ns), []):
                if not _parent(n) or is_dir(remote, _parent(n)):
                    cands.setdefault(p, []).append(n)
    renamed_on_nas: set[str] = set()
    for g, n in _unique_pairs(cands):
        if posixpath.basename(g) == posixpath.basename(n):
            moves.append(("local", g, n))          # solo movido: moveItems, el nombre no cambia
        else:
            renamed_on_nas.add(g)

    # ficheros movidos en iCloud: falta en iCloud, intacto en el NAS; nuevo en iCloud con mismo tamaño+fecha
    cands = {}
    new_by_key = {}
    for p, e in remote.items():
        if not e.is_dir and e.size and p not in snapshot and p not in local and not covered(p):
            new_by_key.setdefault((e.size, round(e.mtime)), []).append(p)
    for p, s in snapshot.items():
        l = local.get(p)
        if (not s.is_dir and s.r_size and p not in remote and l is not None and not l.is_dir
                and l.size == s.l_size and l.mtime_ns == s.l_mtime_ns and not covered(p)):
            for n in new_by_key.get((s.r_size, round(s.r_mtime)), []):
                if not _parent(n) or is_dir(local, _parent(n)):
                    cands.setdefault(p, []).append(n)
    moves += [("remote", g, n) for g, n in _unique_pairs(cands)]
    return moves, renamed_on_nas


def _rebase(d: dict, old: str, new: str, make):
    """Copia de d con lo que cuelga de old movido a new."""
    out = {}
    for p, e in d.items():
        if p == old or p.startswith(old + "/"):
            np = new + p[len(old):]
            out[np] = make(e, np)
        else:
            out[p] = e
    return out


def conflict_name(path: str, taken: set[str], now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H%M")
    head, tail = posixpath.split(path)
    stem, ext = posixpath.splitext(tail)
    n = 1
    while True:
        suffix = f" ({names.CONFLICT} {stamp})" if n == 1 else f" ({names.CONFLICT} {stamp} {n})"
        candidate = posixpath.join(head, f"{stem}{suffix}{ext}")
        if candidate not in taken and candidate.casefold() not in {t.casefold() for t in taken}:
            return candidate
        n += 1


class SyncEngine:
    def __init__(
        self,
        db: StateDB,
        local: LocalTree,
        remote: Remote,
        excludes: list[str],
        folders: list[str],
        max_delete: int,
        progress: Progress | None = None,
        renames_file: Path | None = None,
        confirm_deletes_over: int | None = None,
    ):
        # Más de este nº de FICHEROS borrados → lote pendiente de confirmación (las carpetas no cuentan)
        self.confirm_over = max_delete if confirm_deletes_over is None else confirm_deletes_over
        self.progress = progress
        self.renames_file = renames_file
        self.db = db
        self.local = local
        self.remote = remote
        self.excludes = excludes
        self.folders = folders
        self.max_delete = max_delete

    # --- API pública ------------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> RunResult:
        result = RunResult(dry_run=dry_run)
        if not self.local.root.is_dir():
            raise SyncAborted(f"No existe la carpeta local {self.local.root} (¿volumen sin montar?)")

        snapshot = self.db.load_snapshot()
        self._phase("listing_remote")
        remote, rw = self.remote.list_tree(self.excludes, self.folders)
        self._phase("scanning_local")
        local, lw = self.local.scan(self.excludes, self.folders)
        result.warnings.extend(lw + rw)

        # Lo que un lado no ha podido leer o representar no está "borrado": se aparta del plan en
        # los dos lados (y su snapshot se conserva intacto) hasta que se pueda volver a ver.
        ignored = getattr(self.local, "ignored", set()) | getattr(self.remote, "ignored", set())
        if ignored:
            local, remote, snapshot = (
                {p: e for p, e in d.items() if not _under_any(p, ignored)} for d in (local, remote, snapshot)
            )

        self._phase("planning")
        local, remote, snapshot = self._requested_renames(local, remote, snapshot, result, dry_run)
        # Renombrados y movimientos (carpetas y ficheros, en los dos sentidos): se replican en el otro
        # lado en vez de "borrado + nuevo". El resto del plan se calcula como si ya estuvieran hechos.
        self._renames: dict[str, tuple[str, str]] = {}
        rename_actions = []
        moves, self._renamed_on_nas = detect_moves(local, remote, snapshot)
        for side, old, new in moves:
            if side == "remote":          # pasó en iCloud → se aplica en el NAS
                parent = _parent(new)
                old_real = local[old].real
                new_real = posixpath.join(local[parent].real, posixpath.basename(new)) if parent else posixpath.basename(new)
                self._renames[old] = (old_real, new_real)
                local = _rebase(local, old, new, lambda e, np, o=old_real, n=new_real: LocalEntry(
                    np, n + e.real[len(o):], e.is_dir, e.size, e.mtime_ns))
                rename_actions.append(Action("rename_local", old, f"renombrado/movido en iCloud a '{new}'", target=new))
            else:                          # pasó en el NAS → se aplica en iCloud
                remote = _rebase(remote, old, new, lambda e, np: RemoteEntry(
                    np, e.is_dir, e.etag, e.size, e.mtime, e.drivewsid, e.docwsid, e.zone))
                rename_actions.append(Action("rename_remote", old, f"renombrado/movido en el NAS a '{new}'", target=new))
            snapshot = _rebase(snapshot, old, new, lambda e, np: SnapshotEntry(
                np, e.is_dir, e.etag, e.r_size, e.r_mtime, e.l_size, e.l_mtime_ns))

        actions, pw = plan(local, remote, snapshot, self.folders)
        actions = rename_actions + actions
        result.warnings.extend(pw)
        result.plan = actions
        deletes = [a for a in actions if a.kind in DELETE_KINDS]

        # Un lado vacío que obligaría a vaciar el otro es casi siempre un volumen sin montar o una
        # respuesta rota de Apple, no un borrado real: no se toca nada aunque no llegue a MAX_DELETE.
        if not local and any(a.kind in ("delete_remote", "rmdir_remote") for a in deletes):
            raise SyncAborted(
                f"La carpeta local está vacía pero la última sync tenía {len(snapshot)} elementos "
                "(¿volumen sin montar?). No se toca nada. Si de verdad quieres vaciar iCloud, "
                "hazlo desde iCloud."
            )
        if not remote and any(a.kind in ("delete_local", "rmdir_local") for a in deletes):
            raise SyncAborted(
                f"iCloud devuelve un Drive vacío pero la última sync tenía {len(snapshot)} elementos. "
                "No se toca nada."
            )
        actions = self._apply_delete_decisions(actions, local, remote, result, dry_run)

        for a in actions:
            result.counts[a.kind] = result.counts.get(a.kind, 0) + 1
        if self.progress:
            visible = [a for a in actions if a.kind not in SILENT_KINDS]
            self.progress.set_plan(len(visible), sum(self._size(a, local, remote) for a in visible))
        if dry_run:
            for a in actions:
                log.info("[dry-run] %s %s (%s)", a.kind, a.path, a.reason)
            result.message = "Simulación: no se ha modificado nada"
            return result

        self._phase("transferring")
        # En un run real, counts pasa a ser lo HECHO (no lo planificado): importa si se para o falla algo.
        result.counts = {k: v for k, v in result.counts.items() if k == "rename_both"}
        self._execute(actions, local, remote, result)
        if getattr(self, "_decisions_used", None):
            self.db.clear_decisions(self._decisions_used)
        if result.status == "ok" and result.errors:
            result.status = "partial"
        return result

    def _apply_delete_decisions(self, actions, local, remote, result: RunResult, dry_run: bool):
        """Borrados: los confirmados por el usuario se ejecutan; los descartados se invierten (se trae
        el fichero del lado donde sigue); los nuevos se ejecutan si son pocos y, si superan
        MAX_DELETE, se retienen TODOS como lote pendiente de confirmar mientras el resto sigue."""
        decided = self.db.decisions()
        self._decisions_used: dict = {}
        final, new_deletes = [], []
        for a in actions:
            if a.kind not in DELETE_KINDS:
                final.append(a)
                continue
            d = decided.get((a.kind, a.path))
            if d == "confirmed":
                final.append(Action(a.kind, a.path, "borrado confirmado por ti"))
                self._decisions_used[(a.kind, a.path)] = d
            elif d == "rejected":
                final.append(Action(INVERSE[a.kind], a.path, "borrado descartado por ti: se recupera"))
                self._decisions_used[(a.kind, a.path)] = d
            else:
                new_deletes.append(a)
        # decisiones que ya no aplican (p.ej. lo recuperaste a mano): también se limpian
        for key, d in decided.items():
            self._decisions_used.setdefault(key, d)

        # Lo que ya está en un lote pendiente sigue pendiente hasta que el usuario lo decida, aunque
        # tras decidir otras carpetas quede por debajo de MAX_DELETE (si no, se borraría solo).
        # Ficheros renombrados en el NAS: su contenido se sube con el nombre nuevo; borrar el viejo
        # en iCloud no pierde nada, así que no pide confirmación.
        renamed = getattr(self, "_renamed_on_nas", set())
        final += [a for a in new_deletes if a.kind == "delete_remote" and a.path in renamed]
        new_deletes = [a for a in new_deletes if not (a.kind == "delete_remote" and a.path in renamed)]
        waiting = self.db.pending_keys()
        held = [a for a in new_deletes if (a.kind, a.path) in waiting]
        fresh = [a for a in new_deletes if (a.kind, a.path) not in waiting]
        if sum(1 for a in fresh if not a.kind.startswith("rmdir")) <= self.confirm_over:
            if not held:
                if not dry_run:
                    self.db.clear_pending_batch()
                return final + fresh
            final = final + fresh
            new_deletes = held

        def size(a):
            e = (remote if a.kind == "delete_remote" else local).get(a.path)
            return e.size if e is not None and not e.is_dir else 0

        files = [a for a in new_deletes if not a.kind.startswith("rmdir")]
        by_side = {s: sum(1 for a in files if DELETE_SIDE[a.kind] == s) for s in ("icloud", "nas")}
        n_dirs = len(new_deletes) - len(files)
        result.pending_deletes = len(new_deletes)
        result.status = "pending"
        result.message = (
            f"{len(files)} ficheros{f' y {n_dirs} carpetas' if n_dirs else ''} borrados esperan tu "
            f"confirmación en el panel ({by_side['icloud']} en iCloud, {by_side['nas']} en el NAS); "
            "el resto se sincroniza"
        )
        log.warning("%s. Primeros: %s", result.message, ", ".join(a.path for a in new_deletes[:5]))
        if not dry_run:
            self.db.set_pending_batch([(a.kind, a.path, size(a), DELETE_SIDE[a.kind]) for a in new_deletes])
        return final

    def _requested_renames(self, local, remote, snapshot, result: RunResult, dry_run: bool):
        """Renombrados pedidos a mano en data/RENOMBRAR.json: {"renombrar": [{"de": ..., "a": ...}]}.
        Se hacen en iCloud y en el NAS a la vez (si solo se cambiara un lado, la sync lo tomaría por
        borrado + carpeta nueva). Cada uno se comprueba antes; si algo no cuadra, no se toca."""
        if not self.renames_file or not self.renames_file.exists():
            return local, remote, snapshot
        try:
            items = json.loads(self.renames_file.read_text(encoding="utf-8")).get("renombrar", [])
        except (OSError, ValueError) as exc:
            result.errors.append(f"{self.renames_file.name} no es un JSON válido: {exc}")
            return local, remote, snapshot
        failed = False
        for it in items:
            old, new = it.get("de", ""), it.get("a", "")
            if new in remote and new in local and old not in remote and old not in local:
                continue                                        # ya hecho en un run anterior
            problem = (
                "el origen no está como carpeta en iCloud" if not (old in remote and remote[old].is_dir) else
                "el origen no está como carpeta en el NAS" if not (old in local and local[old].is_dir) else
                "el destino ya existe" if (new in remote or new in local) else
                "solo se renombra dentro de la misma carpeta" if _parent(old) != _parent(new) else
                "nombre nuevo vacío" if not posixpath.basename(new).strip() else ""
            )
            if problem:
                result.warnings.append(f"No se renombra '{old}' → '{new}': {problem}")
                failed = True
                continue
            parent = _parent(new)
            old_real = local[old].real
            new_real = posixpath.join(local[parent].real, posixpath.basename(new)) if parent else posixpath.basename(new)
            if dry_run:
                log.info("[dry-run] rename_both '%s' → '%s' (in iCloud and on the NAS)", old, new)
            else:
                try:
                    if self.progress:
                        self.progress.action_start("rename_both", new, 0)
                    self.remote.rename_dir(old, new)
                    self.local.rename(old_real, new_real)
                    self.db.rename_prefix(old, new)
                    log.info("rename_both '%s' → '%s' (in iCloud and on the NAS)", old, new)
                    if self.progress:
                        self.progress.action_done("rename_both", new, 0, True, reason=f"antes '{old}'")
                except AuthRequired:
                    raise
                except Exception as exc:
                    msg = f"rename_both {old} → {new}: {type(exc).__name__}: {exc}"
                    log.warning(msg)
                    result.errors.append(msg)
                    failed = True
                    if self.progress:
                        self.progress.action_done("rename_both", new, 0, False, error=str(exc))
                    continue
            result.counts["rename_both"] = result.counts.get("rename_both", 0) + 1
            local = _rebase(local, old, new, lambda e, np, o=old_real, n=new_real: LocalEntry(
                np, n + e.real[len(o):], e.is_dir, e.size, e.mtime_ns))
            remote = _rebase(remote, old, new, lambda e, np: RemoteEntry(
                np, e.is_dir, e.etag, e.size, e.mtime, e.drivewsid, e.docwsid, e.zone))
            snapshot = _rebase(snapshot, old, new, lambda e, np: SnapshotEntry(
                np, e.is_dir, e.etag, e.r_size, e.r_mtime, e.l_size, e.l_mtime_ns))
        if not dry_run and not failed:
            os.replace(self.renames_file, self.renames_file.with_name("RENOMBRAR.hecho.json"))
        return local, remote, snapshot

    def _phase(self, phase: str) -> None:
        if self.progress:
            self.progress.set_phase(phase)

    @staticmethod
    def _size(a: Action, local: dict, remote: dict) -> int:
        """Bytes que mueve una acción (para la barra de progreso)."""
        if a.kind == "download":
            return remote[a.path].size
        if a.kind in ("upload", "replace"):
            return local[a.path].size
        if a.kind == "conflict":
            return remote[a.path].size + local[a.path].size
        return 0

    # --- ejecución --------------------------------------------------------------------------

    def _execute(self, actions, local, remote, result: RunResult) -> None:
        self._local = local
        self._remote = remote
        phases = [
            [a for a in actions if a.kind in ("rename_local", "rename_remote")],
            sorted([a for a in actions if a.kind in ("mkdir_local", "mkdir_remote")], key=lambda a: _depth(a.path)),
            [a for a in actions if a.kind in ("conflict", "download", "upload", "replace", "adopt")],
            [a for a in actions if a.kind in ("delete_local", "delete_remote", "forget")],
            sorted([a for a in actions if a.kind in ("rmdir_local", "rmdir_remote")], key=lambda a: -_depth(a.path)),
        ]
        progress = self.progress
        for phase in phases:
            for a in phase:
                if progress and progress.stop_requested:
                    # Entre dos acciones: el snapshot ya refleja todo lo hecho, el siguiente run sigue aquí.
                    result.status = "stopped"
                    result.message = "Parada a petición del usuario; el siguiente run continúa donde se quedó"
                    log.info("Sync stopped at the user's request")
                    return
                silent = a.kind in SILENT_KINDS
                size = self._size(a, local, remote)
                if progress and not silent:
                    progress.action_start(a.kind, a.path, size)
                try:
                    if a.kind == "rename_local":
                        self._do_rename_local(a, result)
                    elif a.kind == "rename_remote":
                        self._do_rename_remote(a, result)
                    else:
                        getattr(self, f"_do_{a.kind}")(a.path, result)
                    log.info("%s %s (%s)", a.kind, a.path, a.reason)
                    result.counts[a.kind] = result.counts.get(a.kind, 0) + 1
                    if progress and not silent:
                        progress.action_done(a.kind, a.path, size, True, reason=a.reason)
                except AuthRequired:
                    raise
                except Exception as exc:  # un fichero que falla no tumba el run
                    msg = f"{a.kind} {a.path}: {type(exc).__name__}: {exc}"
                    log.warning(msg)
                    result.errors.append(msg)
                    if progress and not silent:
                        progress.action_done(a.kind, a.path, size, False, error=f"{type(exc).__name__}: {exc}")

    def _real(self, path: str) -> str:
        """Ruta en disco: la real si ya existe (puede ser NFD), si no la del padre + nombre."""
        e = self._local.get(path)
        if e is not None:
            return e.real
        parent = _parent(path)
        name = posixpath.basename(path)
        return posixpath.join(self._real(parent), name) if parent else name

    def _snap_file(self, path: str, l: LocalEntry, r: RemoteEntry | None) -> None:
        self.db.put(SnapshotEntry(
            path, False,
            etag=r.etag if r else None,
            r_size=r.size if r else l.size,
            r_mtime=r.mtime if r else l.mtime_ns / 1e9,
            l_size=l.size, l_mtime_ns=l.mtime_ns,
        ))

    def _do_rename_local(self, a: Action, result: RunResult) -> None:
        old_real, new_real = self._renames[a.path]
        if self.local.exists(new_real):
            raise FileExistsError(f"ya existe {new_real} en el NAS")
        self.local.rename(old_real, new_real)
        self.db.rename_prefix(a.path, a.target)

    def _do_rename_remote(self, a: Action, result: RunResult) -> None:
        self.remote.move(a.path, a.target)
        self.db.rename_prefix(a.path, a.target)

    def _do_mkdir_local(self, path: str, result: RunResult) -> None:
        real = self._real(path)
        self.local.mkdir(real)
        self._local[path] = LocalEntry(path, real, True)
        self.db.put(SnapshotEntry(path, True))

    def _do_mkdir_remote(self, path: str, result: RunResult) -> None:
        self._remote[path] = self.remote.mkdir(path)
        self.db.put(SnapshotEntry(path, True))

    def _do_adopt(self, path: str, result: RunResult) -> None:
        l, r = self._local[path], self._remote[path]
        if l.is_dir:
            self.db.put(SnapshotEntry(path, True))
        else:
            self._snap_file(path, l, r)

    def _do_download(self, path: str, result: RunResult) -> None:
        r = self._remote[path]
        real = self._real(path)
        tmp = self.local.tmp_path_for(real)
        try:
            self.remote.download(r, tmp)
            l = self.local.commit_download(tmp, real, r.mtime)
        finally:
            tmp.unlink(missing_ok=True)
        self._local[path] = LocalEntry(path, real, False, l.size, l.mtime_ns)
        self._snap_file(path, l, r)

    def _do_upload(self, path: str, result: RunResult) -> None:
        l = self._local[path]
        r = self.remote.upload(self.local.abs(l.real), path, l.mtime_ns / 1e9)
        if self.progress:
            self.progress.add_bytes(l.size, upload=True)
        if r is not None:
            self._remote[path] = r
        self._snap_file(path, l, r)

    def _do_replace(self, path: str, result: RunResult) -> None:
        # upload() nunca sobrescribe (iCloud crearía "nombre 2"): la versión vieja va a
        # Eliminados recientemente y se sube la nueva. Si la subida falla, el siguiente run ve
        # "borrado en iCloud pero cambiado en local" y la vuelve a subir.
        self.remote.trash(self._remote.pop(path))
        self._do_upload(path, result)

    def _do_conflict(self, path: str, result: RunResult) -> None:
        taken = set(self._local) | set(self._remote)
        copy = conflict_name(path, taken)
        l = self._local.pop(path)
        copy_real = posixpath.join(posixpath.dirname(l.real), posixpath.basename(copy))
        self.local.rename(l.real, copy_real)
        st = self.local.stat(copy_real)
        self._local[copy] = LocalEntry(copy, copy_real, False, st.size, st.mtime_ns)
        self.db.add_conflict(path, copy)
        result.conflicts.append(f"{path} → {copy}")
        # La versión de iCloud se queda con el nombre original; la del NAS se sube como copia.
        self._do_download(path, result)
        self._do_upload(copy, result)

    def _do_delete_local(self, path: str, result: RunResult) -> None:
        self.local.to_trash(self._local.pop(path).real)
        self.db.forget(path)

    def _do_delete_remote(self, path: str, result: RunResult) -> None:
        self.remote.trash(self._remote.pop(path))
        self.db.forget(path)

    def _do_forget(self, path: str, result: RunResult) -> None:
        self.db.forget(path)

    def _do_rmdir_local(self, path: str, result: RunResult) -> None:
        real = self._local.pop(path).real
        # Puede quedar algo excluido dentro (.DS_Store, @eaDir...): entonces a la papelera entera.
        if not self.local.rmdir_if_empty(real):
            self.local.to_trash(real)
        self.db.forget(path)

    def _do_rmdir_remote(self, path: str, result: RunResult) -> None:
        self.remote.trash(self._remote.pop(path))
        self.db.forget(path)
