# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Tipos compartidos entre el motor de sync y los dos lados (local / iCloud)."""

from dataclasses import dataclass, field


@dataclass
class LocalEntry:
    path: str          # ruta relativa, NFC, separador "/"
    real: str          # ruta relativa tal y como está en disco (puede ser NFD)
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0


@dataclass
class RemoteEntry:
    path: str
    is_dir: bool
    etag: str | None = None
    size: int = 0
    mtime: float = 0.0  # dateModified en epoch (UTC)
    drivewsid: str = ""
    docwsid: str = ""
    zone: str = ""


@dataclass
class SnapshotEntry:
    path: str
    is_dir: bool
    etag: str | None = None   # None = subido pero sin etag confirmado todavía
    r_size: int = 0
    r_mtime: float = 0.0
    l_size: int = 0
    l_mtime_ns: int = 0


@dataclass
class Action:
    kind: str   # mkdir_local, mkdir_remote, download, upload, replace, conflict, adopt,
                # delete_local, delete_remote, rmdir_local, rmdir_remote, forget
    path: str
    reason: str = ""
    target: str = ""   # rename_local: ruta nueva


@dataclass
class RunResult:
    status: str = "ok"          # ok | partial | pending | aborted | error | skipped | stopped
    dry_run: bool = False
    message: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)     # operaciones fallidas -> status "partial"
    warnings: list[str] = field(default_factory=list)   # cosas ignoradas a propósito, no son fallos
    conflicts: list[str] = field(default_factory=list)
    plan: list[Action] = field(default_factory=list)
    pending_deletes: int = 0    # borrados retenidos esperando confirmación del usuario
    mode: str = ""              # Fotos: backfill | changes | full


class AuthRequired(Exception):
    """La sesión de Apple ya no vale (hace falta 2FA o volver a hacer login). Aborta el run entero."""


class SyncAborted(Exception):
    """Una salvaguarda ha parado el run antes de tocar nada."""
