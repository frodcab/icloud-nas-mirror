# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Límites por run de la sync de iCloud Fotos, para no provocar el bloqueo de Apple.

Una fototeca grande (≈200 GB, decenas de miles de fotos) no se baja en un run: cada run hace como
mucho PHOTOS_MAX_* operaciones o bytes, con una pausa entre cada una, y lo que queda sale en el
siguiente. Si Apple empieza a frenar (429/503) varias veces seguidas, el run se corta y el siguiente
espera más (el doble cada vez, hasta MAX_BACKOFF); un run sin frenazos vuelve al intervalo normal.
"""

import time
from dataclasses import dataclass, field
from typing import Callable

KINDS = ("descargas", "subidas", "borrados")
MAX_BACKOFF = 8          # el intervalo como mucho se multiplica por esto (60 min → 8 h)


@dataclass
class PhotosLimits:
    max_downloads: int = 1000          # fotos/vídeos descargados por run
    max_bytes: int = 10 * 2**30        # bytes descargados + subidos por run
    max_uploads: int = 200
    max_deletes: int = 200             # aparte de la confirmación por encima de CONFIRM_DELETES_OVER
    pause_s: float = 0.5               # entre transferencias
    list_pause_s: float = 0.2          # entre páginas al listar la fototeca o los cambios
    max_throttles: int = 3             # frenazos de Apple (429/503) que se aguantan en un run


@dataclass
class RunBudget:
    """Presupuesto de UN run. El motor pregunta allow() antes de cada operación y llama a spend()
    después; si allow() dice que no, la operación se deja para el run siguiente (no es un error)."""

    limits: PhotosLimits
    sleep: Callable[[float], None] = time.sleep
    used: dict = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    bytes_used: int = 0
    throttles: int = 0
    deferred: dict = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    stop_reason: str = ""

    def _cap(self, kind: str) -> int:
        return {"descargas": self.limits.max_downloads, "subidas": self.limits.max_uploads,
                "borrados": self.limits.max_deletes}[kind]

    @property
    def exhausted(self) -> bool:
        return bool(self.stop_reason)

    def allow(self, kind: str, size: int = 0) -> bool:
        if self.stop_reason:
            ok = False
        elif self.used[kind] >= self._cap(kind):
            ok = False
        elif size and self.bytes_used and self.bytes_used + size > self.limits.max_bytes:
            # El primer fichero siempre pasa, aunque solo él supere el tope (un vídeo enorme)
            ok = False
        else:
            ok = True
        if not ok:
            self.deferred[kind] += 1
        return ok

    def spend(self, kind: str, size: int = 0) -> None:
        self.used[kind] += 1
        self.bytes_used += size
        if self.limits.pause_s:
            self.sleep(self.limits.pause_s)

    def page(self) -> None:
        """Una página de listado: solo pausa (listar no gasta presupuesto de transferencias)."""
        if self.limits.list_pause_s:
            self.sleep(self.limits.list_pause_s)

    def throttled(self, detail: str = "") -> bool:
        """Apple ha frenado (tras agotar los reintentos). True = cortar el run ya."""
        self.throttles += 1
        if self.throttles >= self.limits.max_throttles and not self.stop_reason:
            self.stop_reason = (f"Apple está frenando ({self.throttles} veces en este run{': ' + detail if detail else ''}); "
                                "se sigue en la próxima vuelta, con más espera")
        return self.exhausted

    def summary(self) -> str:
        done = ", ".join(f"{self.used[k]} {k}" for k in KINDS if self.used[k])
        left = ", ".join(f"{self.deferred[k]} {k}" for k in KINDS if self.deferred[k])
        text = f"{done or 'nada'} ({self.bytes_used / 2**30:.1f} GB)".replace(".", ",")
        if left:
            text += f"; quedan para la próxima vuelta: {left}"
        return text + (f". {self.stop_reason}" if self.stop_reason else "")


class Backoff:
    """Multiplicador del intervalo entre runs de Fotos: se dobla tras un run cortado por frenazos."""

    def __init__(self):
        self.factor = 1

    def after_run(self, budget: RunBudget) -> int:
        self.factor = min(self.factor * 2, MAX_BACKOFF) if budget.throttles >= budget.limits.max_throttles else 1
        return self.factor
