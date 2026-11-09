# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Vigilante de una vuelta de sync (Drive o Fotos), compartido por los dos servicios.

El servicio que lo usa aporta `running`, `progress`, `db` (con get/set), `notifier`, `public_url` y `_stop`.
Una vuelta está mal si:
- lleva `stall_s` sin señales de vida (ni páginas, ni trozos, ni llamadas a Apple que vuelvan), o
- lleva más de `run_max_s` en marcha aunque no pare de llamar a Apple (el 5/11 una vuelta de Fotos estuvo
  12 h sin bajar nada: cada llamada contaba como latido).
Entonces se pide parar y se avisa por correo; si `kill_grace_s` después sigue ahí, se reinicia el proceso
(Docker lo levanta; el estado está en SQLite y la sync continúa donde se quedó).
"""

import logging
import os
import threading
import time
from datetime import datetime

from core.i18n import translate_line

log = logging.getLogger(__name__)


class WatchdogMixin:
    def init_watchdog(self, name: str, *, stall_s: float, kill_grace_s: float, run_max_s: float,
                      watch_every_s: float = 30, exit_fn=None) -> None:
        self.wd_name = name
        self.stall_s, self.kill_grace_s, self.watch_every_s = stall_s, kill_grace_s, watch_every_s
        self.run_max_s = run_max_s     # tope de duración de una vuelta, haya o no latido (0 = sin tope)
        self._exit = exit_fn or (lambda: os._exit(3))
        self._stalled_at: float | None = None

    def start_watchdog(self) -> None:
        if self.stall_s > 0 or self.run_max_s > 0:
            threading.Thread(target=self._watch, name=f"{self.wd_name.lower()}-watchdog", daemon=True).start()

    def _watch(self) -> None:
        while not self._stop:
            time.sleep(self.watch_every_s)
            try:
                self.check_stall()
            except Exception:
                log.exception("Error in the %s watchdog", self.wd_name)

    def check_stall(self, now: float | None = None) -> str:
        """Una pasada del vigilante. Devuelve ok | colgada | reinicio (para las pruebas)."""
        now = time.time() if now is None else now
        if not self.running:
            self._stalled_at = None
            return "ok"
        quiet = now - self.progress.last_activity
        started = self.progress.started_at or now
        stalled = self.stall_s > 0 and quiet >= self.stall_s
        too_long = self.run_max_s > 0 and now - started >= self.run_max_s
        if not stalled and not too_long:
            self._stalled_at = None
            return "ok"
        if self._stalled_at is None:
            self._stalled_at = now
            phase = self.progress.phase
            why = (f"lleva {int(quiet // 60)} min sin dar señales de vida" if stalled else
                   f"lleva {int((now - started) // 60)} min en marcha (tope {int(self.run_max_s // 60)} min)")
            log.error("%s: the run %s (phase %s); asking it to stop", self.wd_name, translate_line(why, "en"), phase)
            self.progress.stop_requested = True
            self._alert(f"{self.wd_name}: la sincronización se ha quedado colgada",
                        f"La vuelta de {self.wd_name} {why} (fase: {phase}). "
                        f"Se ha pedido parar; si no responde en {int(self.kill_grace_s // 60)} min se reinicia "
                        f"la aplicación. No se pierde nada: el estado está guardado y la sync continúa.")
            return "colgada"
        if now - self._stalled_at >= self.kill_grace_s:
            log.error("%s: the run is still stuck after %d s; restarting the process", self.wd_name, self.kill_grace_s)
            self.db.set("restarted_by_watchdog", datetime.now().isoformat(timespec="seconds"))
            self._exit()
            return "reinicio"
        return "colgada"

    def _alert(self, subject: str, body: str) -> None:
        link = f"\nPanel: {self.public_url}" if self.public_url else ""
        # El correo puede tardar (SMTP): en un hilo aparte y con tope, para que el vigilante no se cuelgue también
        t = threading.Thread(target=lambda: self.notifier.send(subject, body + link), daemon=True)
        t.start()
        t.join(timeout=45)
