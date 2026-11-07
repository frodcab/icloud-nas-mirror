# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Vigilante de la sync de Fotos: una vuelta colgada se para, se avisa y, si no responde, se reinicia."""

import time

from services.photos_service import PhotosSyncService
from services.progress import Progress


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, to=None):
        self.sent.append(subject)
        return True


class _DB:
    def __init__(self):
        self.kv = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v):
        self.kv[k] = v


def _svc(exits):
    p = Progress()
    s = PhotosSyncService(db=_DB(), auth=None, engine=None, notifier=_Notifier(), interval_min=240,
                          dry_run=True, progress=p, stall_s=1200, kill_grace_s=300,
                          exit_fn=lambda: exits.append(1))
    return s, p


def test_sin_vuelta_en_marcha_no_hace_nada():
    exits = []
    s, _ = _svc(exits)
    assert s.check_stall(time.time() + 99999) == "ok" and not exits


def test_vuelta_con_latido_reciente_no_se_toca():
    exits = []
    s, p = _svc(exits)
    s.running = True
    p.touch()
    assert s.check_stall(time.time() + 600) == "ok" and not p.stop_requested


def test_colgada_pide_parar_avisa_y_luego_reinicia():
    exits = []
    s, p = _svc(exits)
    s.running = True
    t0 = p.last_activity
    assert s.check_stall(t0 + 1300) == "colgada"
    assert p.stop_requested and s.notifier.sent == ["Fotos: la sincronización se ha quedado colgada"]
    assert s.check_stall(t0 + 1400) == "colgada" and not exits          # en la cortesía, sin avisar de nuevo
    assert len(s.notifier.sent) == 1
    assert s.check_stall(t0 + 1300 + 301) == "reinicio" and exits == [1]
    assert s.db.get("restarted_by_watchdog")


def test_si_se_recupera_en_la_cortesia_no_reinicia():
    exits = []
    s, p = _svc(exits)
    s.running = True
    t0 = p.last_activity
    s.check_stall(t0 + 1300)
    p.touch()
    assert s.check_stall(time.time() + 10) == "ok" and not exits and s._stalled_at is None


def test_touch_en_cada_avance():
    p = Progress()
    for fn in (lambda: p.set_phase("listing"), lambda: p.add_bytes(10), lambda: p.folder_scanned("x", 1),
               lambda: p.add_log("x"), lambda: p.action_start("download", "a", 1)):
        p.last_activity = 0
        fn()
        assert p.last_activity > 0


def test_vuelta_demasiado_larga_aunque_haya_latido_se_para():
    exits = []
    s, p = _svc(exits)
    s.running, s.run_max_s = True, 10800
    p.started_at = time.time() - 11000
    p.touch()                                   # sigue llamando a Apple, pero lleva más de 3 h
    assert s.check_stall() == "colgada" and p.stop_requested
    assert "en marcha" in s.notifier.sent[0] or s.notifier.sent


def test_vigilante_de_drive_mismo_comportamiento():
    from services.scheduler import SyncService
    exits = []
    p = Progress()
    s = SyncService.__new__(SyncService)
    s.db, s.notifier, s.progress, s.public_url, s.running, s._stop = _DB(), _Notifier(), p, "", True, False
    s.init_watchdog("Drive", stall_s=2700, kill_grace_s=300, run_max_s=21600, exit_fn=lambda: exits.append(1))
    t0 = p.last_activity
    assert s.check_stall(t0 + 100) == "ok"
    assert s.check_stall(t0 + 2800) == "colgada" and s.notifier.sent == ["Drive: la sincronización se ha quedado colgada"]
    assert s.check_stall(t0 + 2800 + 301) == "reinicio" and exits == [1]
