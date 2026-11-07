# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Progreso en vivo: fases, totales, feed de actividad, log incremental y parada a mitad de run."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sync_engine import EXCLUDES, FakeRemote, write  # noqa: E402

from services.local_fs import LocalTree  # noqa: E402
from services.progress import Progress  # noqa: E402
from services.state_db import StateDB  # noqa: E402
from services.sync_engine import SyncEngine  # noqa: E402


class RecordingProgress(Progress):
    def __init__(self):
        super().__init__()
        self.phases = []

    def set_phase(self, phase):
        self.phases.append(phase)
        super().set_phase(phase)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    remote = FakeRemote()
    progress = RecordingProgress()
    engine = SyncEngine(StateDB(tmp_path / "s.db"), LocalTree(root, tmp_path / "trash"), remote,
                        EXCLUDES, [], 100, progress=progress)
    return root, remote, progress, engine


def test_phases_totals_and_activity(env):
    root, remote, progress, engine = env
    remote.put("docs/a.pdf", b"x" * 1000)
    write(root, "nuevo.txt", b"y" * 10)
    progress.start(dry_run=False)
    res = engine.run()
    assert res.status == "ok"
    assert progress.phases == ["listing_remote", "scanning_local", "planning", "transferring"]
    snap = progress.snapshot()
    # mkdir_local docs + download a.pdf + upload nuevo.txt; los "adopt" internos no cuentan
    assert snap["actions_total"] == 3 and snap["actions_done"] == 3
    assert snap["bytes_total"] == 1010
    assert snap["counts_done"] == {"mkdir_local": 1, "download": 1, "upload": 1}
    feed = progress.activity()
    assert [a["kind"] for a in feed] == ["mkdir_local", "download", "upload"]
    assert all(a["ok"] for a in feed)
    # incremental: solo lo posterior a un seq
    assert [a["kind"] for a in progress.activity(since=feed[0]["seq"])] == ["download", "upload"]


def test_failed_action_is_in_feed_as_error(env):
    root, remote, progress, engine = env
    write(root, "a.txt", b"1")
    remote.fail_upload.add("a.txt")
    progress.start(dry_run=False)
    res = engine.run()
    assert res.status == "partial"
    last = progress.activity()[-1]
    assert not last["ok"] and "upload falla" in last["error"]
    assert progress.snapshot()["errors"] == 1


def test_stop_between_actions_keeps_snapshot_and_resumes(env):
    root, remote, progress, engine = env
    for i in range(5):
        remote.put(f"f{i}.txt", b"x")
    original = remote.download

    def download_then_stop(entry, dest):
        original(entry, dest)
        progress.stop_requested = True

    remote.download = download_then_stop
    progress.start(dry_run=False)
    res = engine.run()
    assert res.status == "stopped"
    assert len(list(root.iterdir())) == 1
    assert res.counts == {"download": 1}     # lo hecho, no lo planificado

    remote.download = original
    progress.start(dry_run=False)          # start() limpia la petición de parada
    res = engine.run()
    assert res.status == "ok" and res.counts == {"download": 4}


def test_log_is_incremental_and_resets_per_run():
    p = Progress()
    p.start(False)
    p.add_log("uno")
    p.add_log("dos")
    first = p.log_lines()
    assert [line for _, line in first] == ["uno", "dos"]
    assert [line for _, line in p.log_lines(since=first[0][0])] == ["dos"]
    p.start(False)
    p.add_log("tres")
    assert [line for _, line in p.log_lines()] == ["tres"]
    assert p.log_lines()[0][0] > first[-1][0]   # el seq sigue creciendo entre runs


def test_speed_and_eta():
    p = Progress()
    p.start(False)
    p.set_plan(2, 10_000_000)
    p.set_phase("transferring")
    p._speed_samples.clear()
    p._speed_samples.extend([(100.0, 0), (110.0, 1_000_000)])   # 100 kB/s
    p.bytes_done = 1_000_000
    import time as _t
    real = _t.time
    _t.time = lambda: 111.0
    try:
        snap = p.snapshot()
    finally:
        _t.time = real
    assert snap["speed_bps"] == 100_000
    assert snap["eta_s"] == 90
