# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Interruptor de simulación forzada (data/SIMULACION)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.entries import RunResult  # noqa: E402
from services.local_fs import LocalTree  # noqa: E402
from services.notify import EmailNotifier  # noqa: E402
from services.progress import Progress  # noqa: E402
from services.scheduler import SyncService  # noqa: E402
from services.state_db import StateDB  # noqa: E402


class FakeAuth:
    state, message, delivery, on_change = "ok", "", "", None

    def connect(self):
        return self.state


class SpyEngine:
    def __init__(self):
        self.calls = []

    def run(self, dry_run=False):
        self.calls.append(dry_run)
        return RunResult(dry_run=dry_run)


def make(tmp_path, flag: bool):
    flag_file = tmp_path / "SIMULACION"
    if flag:
        flag_file.write_text("x")
    engine = SpyEngine()
    svc = SyncService(db=StateDB(tmp_path / "s.db"), auth=FakeAuth(), engine=engine,
                      local=LocalTree(tmp_path, tmp_path / "t"), notifier=EmailNotifier("", 0, "", "", ""),
                      interval_min=15, dry_run=False, trash_days=30, state_dir=tmp_path, public_url="",
                      progress=Progress(), force_dry_run_files=[flag_file])
    return svc, engine


def test_flag_file_forces_dry_run_even_on_explicit_real_run(tmp_path):
    svc, engine = make(tmp_path, flag=True)
    svc.run_once(dry_run=False)
    assert engine.calls == [True]
    assert svc.status()["forced_dry_run"] is True


def test_without_flag_runs_normally(tmp_path):
    svc, engine = make(tmp_path, flag=False)
    svc.run_once(dry_run=False)
    assert engine.calls == [False]


class SpyNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


class PendingEngine:
    def __init__(self, db):
        self.db = db

    def run(self, dry_run=False):
        if not self.db.pending_items():
            self.db.set_pending_batch([("delete_remote", f"Dieta/f{i}.pdf", 1000, "icloud") for i in range(3)]
                                      + [("rmdir_remote", "Dieta", 0, "icloud")])
        return RunResult(status="pending", pending_deletes=4, message="4 borrados esperan")


def test_pending_batch_notifies_once_and_is_not_a_failure(tmp_path):
    db = StateDB(tmp_path / "s.db")
    notifier = SpyNotifier()
    svc = SyncService(db=db, auth=FakeAuth(), engine=PendingEngine(db),
                      local=LocalTree(tmp_path, tmp_path / "t"), notifier=notifier,
                      interval_min=15, dry_run=False, trash_days=30, state_dir=tmp_path, public_url="",
                      progress=Progress(), force_dry_run_files=[])
    svc.run_once(False)
    svc.run_once(False)
    subjects = [s for s, _ in notifier.sent]
    assert subjects == ["3 ficheros borrados esperan tu confirmación"]
    assert db.get("failing") is None
    summary = svc.pending_summary()
    assert summary["folders"][0]["folder"] == "Dieta" and summary["folders"][0]["count"] == 3
    assert summary["sides"]["icloud"]["count"] == 3 and "en iCloud" in summary["text"]
    assert svc.status()["pending_deletes"] == 4


def test_decide_group_only_touches_that_folder(tmp_path):
    db = StateDB(tmp_path / "s.db")
    batch = db.set_pending_batch([("delete_remote", "A/x.jpg", 10, "icloud"), ("delete_remote", "A/y.jpg", 10, "icloud"),
                                  ("delete_remote", "B/z.jpg", 10, "icloud")])
    svc, _ = make(tmp_path, flag=False)
    svc.db = db
    assert svc.decide_group(batch, "A", "icloud", "rejected") == 2
    assert db.decisions() == {("delete_remote", "A/x.jpg"): "rejected", ("delete_remote", "A/y.jpg"): "rejected"}
    assert [i["path"] for i in db.pending_items()] == ["B/z.jpg"]
    assert svc.decide_group("otro-lote", "B", "icloud", "confirmed") == 0
