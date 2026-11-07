# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Tests del motor contra un iCloud falso en memoria y una carpeta local temporal.

    cd apps/app-integration-icloud-sync && python -m pytest -q tests
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.entries import AuthRequired, RemoteEntry, SyncAborted  # noqa: E402
from services.local_fs import LocalTree, in_scope, is_excluded  # noqa: E402
from services.state_db import StateDB  # noqa: E402
from services.sync_engine import SyncEngine  # noqa: E402

EXCLUDES = [".DS_Store", "@eaDir", "*.tmp"]


class FakeRemote:
    """iCloud en memoria: {ruta: bytes} para ficheros, set para carpetas."""

    def __init__(self):
        self.files: dict[str, tuple[bytes, float, int]] = {}  # ruta -> (contenido, mtime, versión)
        self.dirs: set[str] = set()
        self.trashed: list[str] = []
        self.fail_upload: set[str] = set()
        self.auth_fail = False
        self.renamed: list = []
        self.moved: list = []
        self._v = 0

    # helpers de test
    def put(self, path: str, data: bytes, mtime: float | None = None):
        self._v += 1
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.dirs.add("/".join(parts[:i]))
        self.files[path] = (data, mtime or time.time(), self._v)

    def read(self, path: str) -> bytes:
        return self.files[path][0]

    # interfaz Remote
    def list_tree(self, excludes, folders):
        if self.auth_fail:
            raise AuthRequired("2FA")
        out = {}
        for d in self.dirs:
            if in_scope(d, folders) and not is_excluded(d, excludes):
                out[d] = RemoteEntry(d, True, etag=f"d-{d}")
        for p, (data, mtime, v) in self.files.items():
            if in_scope(p, folders) and not is_excluded(p, excludes):
                out[p] = RemoteEntry(p, False, etag=f"e{v}", size=len(data), mtime=mtime)
        return out, []

    def download(self, entry, dest: Path):
        dest.write_bytes(self.files[entry.path][0])

    def upload(self, source: Path, rel_path, mtime):
        if rel_path in self.fail_upload:
            raise RuntimeError("upload falla")
        assert rel_path not in self.files, "upload no debe sobrescribir nunca"
        self.put(rel_path, source.read_bytes(), mtime)
        data, m, v = self.files[rel_path]
        return RemoteEntry(rel_path, False, etag=f"e{v}", size=len(data), mtime=m)

    def trash(self, entry):
        self.trashed.append(entry.path)
        if entry.is_dir:
            self.dirs.discard(entry.path)
            for p in [p for p in self.files if p.startswith(entry.path + "/")]:
                del self.files[p]
        else:
            del self.files[entry.path]

    def move(self, old, new):
        self.moved.append((old, new))
        if old in self.files:
            self.files[new] = self.files.pop(old)
        else:
            _rename_remote_dir(self, old, new)

    def rename_dir(self, old, new):
        self.renamed.append((old, new))
        _rename_remote_dir(self, old, new)

    def mkdir(self, rel_path):
        self.dirs.add(rel_path)
        return RemoteEntry(rel_path, True, etag=f"d-{rel_path}")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    remote = FakeRemote()
    db = StateDB(tmp_path / "state.db")
    local = LocalTree(root, tmp_path / "trash")

    def make(folders=None, max_delete=100, confirm_over=None):
        return SyncEngine(db, local, remote, EXCLUDES, folders or [], max_delete,
                          renames_file=tmp_path / "RENOMBRAR.json", confirm_deletes_over=confirm_over)

    return root, remote, db, make, tmp_path


def write(root: Path, rel: str, data: bytes, mtime: float | None = None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))


def bump(root: Path, rel: str, data: bytes):
    """Modifica un fichero local garantizando que cambia el mtime."""
    p = root / rel
    old = p.stat().st_mtime
    p.write_bytes(data)
    os.utime(p, (old + 10, old + 10))


def test_first_run_merges_both_sides_without_deleting(env):
    root, remote, db, make, _ = env
    write(root, "local/a.txt", b"L")
    remote.put("remote/b.txt", b"R")
    res = make().run()
    assert res.status == "ok", res.errors
    assert (root / "remote/b.txt").read_bytes() == b"R"
    assert remote.read("local/a.txt") == b"L"
    assert remote.trashed == []
    # segunda pasada: nada que hacer
    assert make().run().counts == {}


def test_download_upload_replace(env):
    root, remote, db, make, _ = env
    write(root, "a.txt", b"1")
    remote.put("b.txt", b"1")
    make().run()

    remote.put("b.txt", b"2")          # cambia en iCloud
    bump(root, "a.txt", b"22")         # cambia en local
    res = make().run()
    assert res.counts == {"download": 1, "replace": 1}
    assert (root / "b.txt").read_bytes() == b"2"
    assert remote.read("a.txt") == b"22"
    assert remote.trashed == ["a.txt"]   # la versión vieja va a Eliminados recientemente
    assert make().run().counts == {}


def test_conflict_keeps_both_versions(env):
    root, remote, db, make, _ = env
    write(root, "doc.txt", b"base")
    make().run()
    bump(root, "doc.txt", b"nas")
    remote.put("doc.txt", b"icloud")
    res = make().run()
    assert res.counts == {"conflict": 1}
    assert (root / "doc.txt").read_bytes() == b"icloud"
    copies = [p for p in os.listdir(root) if "conflicto NAS" in p]
    assert len(copies) == 1 and (root / copies[0]).read_bytes() == b"nas"
    assert remote.read(copies[0]) == b"nas"
    assert db.conflicts()[0]["path"] == "doc.txt"
    assert make().run().counts == {}


def test_new_on_both_sides_same_content_is_adopted(env):
    root, remote, db, make, _ = env
    write(root, "x.bin", b"abc", mtime=1_700_000_000)
    remote.put("x.bin", b"abc", mtime=1_700_000_001)
    assert make().run().counts == {"adopt": 1}


def test_deletions_propagate_to_trash(env):
    root, remote, db, make, tmp = env
    write(root, "dir/l.txt", b"l")
    remote.put("dir/r.txt", b"r")
    make().run()

    (root / "dir/l.txt").unlink()      # borrado en local  -> se borra en iCloud
    remote.trash(RemoteEntry("dir/r.txt", False))  # borrado en iCloud -> papelera local
    remote.trashed.clear()
    res = make().run()
    assert res.counts == {"delete_remote": 1, "delete_local": 1}
    assert remote.trashed == ["dir/l.txt"]
    assert not (root / "dir/r.txt").exists()
    assert list((tmp / "trash").rglob("r.txt"))


def test_delete_vs_change_keeps_changed_side(env):
    root, remote, db, make, _ = env
    write(root, "a.txt", b"1")
    write(root, "b.txt", b"1")
    make().run()
    (root / "a.txt").unlink()
    remote.put("a.txt", b"cambiado")   # borrado en local, cambiado en iCloud -> se recupera
    remote.trash(RemoteEntry("b.txt", False))
    bump(root, "b.txt", b"cambiado")   # borrado en iCloud, cambiado en local -> se resube
    res = make().run()
    assert res.counts == {"download": 1, "upload": 1}
    assert (root / "a.txt").read_bytes() == b"cambiado"
    assert remote.read("b.txt") == b"cambiado"


def test_folder_deleted_locally_is_removed_remotely(env):
    root, remote, db, make, _ = env
    remote.put("viejo/sub/f.txt", b"x")
    remote.put("queda.txt", b"x")  # con la carpeta local vacía, el run se abortaría (ver test_empty_local)
    make().run()
    import shutil
    shutil.rmtree(root / "viejo")
    res = make().run()
    assert res.counts == {"delete_remote": 1, "rmdir_remote": 2}
    assert "viejo" not in remote.dirs and "viejo/sub" not in remote.dirs


def test_folder_deleted_remotely_but_new_local_file_survives(env):
    root, remote, db, make, _ = env
    remote.put("carpeta/f.txt", b"x")
    remote.put("queda.txt", b"x")
    make().run()
    write(root, "carpeta/nuevo.txt", b"n")
    remote.trash(RemoteEntry("carpeta", True))
    res = make().run()
    assert res.counts == {"mkdir_remote": 1, "upload": 1, "delete_local": 1}
    assert remote.read("carpeta/nuevo.txt") == b"n"


def test_mass_delete_is_held_for_confirmation_and_rest_syncs(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"dup/f{i}.txt", b"x")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "dup")                      # el usuario borra duplicados en el NAS
    write(root, "nuevo.txt", b"n")                   # y a la vez crea otra cosa
    res = make(max_delete=3).run()
    assert res.status == "pending" and res.pending_deletes == 6    # 5 ficheros + la carpeta
    assert all(f"dup/f{i}.txt" in remote.files for i in range(5))  # nada borrado en iCloud
    assert remote.read("nuevo.txt") == b"n"                         # el resto sí se sincroniza
    items = db.pending_items()
    assert len(items) == 6 and {i["side"] for i in items} == {"icloud"}
    assert make(max_delete=3).run().status == "pending"            # sigue esperando


def test_confirmed_batch_is_executed_even_over_the_limit(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"dup/f{i}.txt", b"x")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "dup")
    make(max_delete=3).run()
    batch = db.pending_items()[0]["batch_id"]
    assert db.decide_batch(batch, "confirmed") == 6
    res = make(max_delete=3).run()
    assert res.status == "ok"
    assert res.counts == {"delete_remote": 5, "rmdir_remote": 1}
    assert not any(p.startswith("dup/") for p in remote.files)
    assert db.pending_items() == [] and db.decisions() == {}
    assert make(max_delete=3).run().counts == {}


def test_rejected_batch_restores_files_from_icloud(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"dup/f{i}.txt", b"x")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "dup")
    make(max_delete=3).run()
    db.decide_batch(db.pending_items()[0]["batch_id"], "rejected")
    res = make(max_delete=3).run()
    assert res.status == "ok" and res.counts == {"mkdir_local": 1, "download": 5}
    assert all((root / f"dup/f{i}.txt").exists() for i in range(5))
    assert remote.trashed == [] and db.decisions() == {}


def test_rejected_batch_restores_files_deleted_in_icloud(env):
    root, remote, db, make, tmp = env
    for i in range(5):
        write(root, f"dup/f{i}.txt", b"x")
    write(root, "queda.txt", b"q")
    make().run()
    remote.trash(RemoteEntry("dup", True))            # borrados en iCloud
    res = make(max_delete=3).run()
    assert res.status == "pending" and {i["side"] for i in db.pending_items()} == {"nas"}
    assert all((root / f"dup/f{i}.txt").exists() for i in range(5))
    db.decide_batch(db.pending_items()[0]["batch_id"], "rejected")
    res = make(max_delete=3).run()
    assert res.counts == {"mkdir_remote": 1, "upload": 5}
    assert all(f"dup/f{i}.txt" in remote.files for i in range(5))


def test_new_deletes_after_confirmation_count_separately(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"dup/f{i}.txt", b"x")
    for i in range(5):
        remote.put(f"otra/g{i}.txt", b"y")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "dup")
    make(max_delete=3).run()
    db.decide_batch(db.pending_items()[0]["batch_id"], "confirmed")
    shutil.rmtree(root / "otra")                      # borrado nuevo tras confirmar el primero
    res = make(max_delete=3).run()
    assert res.status == "pending"
    assert not any(p.startswith("dup/") for p in remote.files)       # lo confirmado se hizo
    assert all(f"otra/g{i}.txt" in remote.files for i in range(5))   # lo nuevo espera
    assert {i["path"].split("/")[0] for i in db.pending_items()} == {"otra"}


def test_batch_is_replaced_when_plan_changes_and_cleared_when_resolved(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"dup/f{i}.txt", b"x")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "dup")
    make(max_delete=3).run()
    first = db.pending_items()[0]["batch_id"]
    for i in range(5):                               # el usuario lo recupera a mano en el NAS
        write(root, f"dup/f{i}.txt", b"x")
    make(max_delete=3).run()                          # ya no hay borrados masivos: lote retirado
    assert db.pending_items() == [] and first


def test_empty_local_after_sync_aborts(env):
    root, remote, db, make, _ = env
    remote.put("a.txt", b"x")
    make().run()
    (root / "a.txt").unlink()   # simula el volumen sin montar: carpeta vacía
    with pytest.raises(SyncAborted):
        make().run()
    assert "a.txt" in remote.files


def test_empty_remote_after_sync_aborts(env):
    root, remote, db, make, _ = env
    write(root, "a.txt", b"x")
    make().run()
    remote.files.clear()        # simula una respuesta vacía de Apple
    with pytest.raises(SyncAborted):
        make().run()
    assert (root / "a.txt").exists()


def test_missing_local_root_aborts(env):
    root, remote, db, make, _ = env
    root.rmdir()
    with pytest.raises(SyncAborted):
        make().run()


def test_failed_upload_does_not_lose_the_file_and_retries(env):
    root, remote, db, make, _ = env
    write(root, "a.txt", b"1")
    make().run()
    bump(root, "a.txt", b"2")
    remote.fail_upload.add("a.txt")
    res = make().run()             # trash OK, upload falla
    assert res.status == "partial"
    assert (root / "a.txt").read_bytes() == b"2"
    remote.fail_upload.clear()
    res = make().run()             # iCloud "borrado" + local cambiado -> se resube
    assert res.counts == {"upload": 1}
    assert remote.read("a.txt") == b"2"


def test_dry_run_changes_nothing(env):
    root, remote, db, make, _ = env
    write(root, "a.txt", b"1")
    remote.put("b.txt", b"1")
    res = make().run(dry_run=True)
    assert res.counts == {"upload": 1, "download": 1}
    assert not (root / "b.txt").exists() and "a.txt" not in remote.files
    assert db.snapshot_count() == 0


def test_excludes_and_scope(env):
    root, remote, db, make, _ = env
    write(root, "Docs/a.txt", b"1")
    write(root, "Docs/.DS_Store", b"x")
    write(root, "Docs/@eaDir/thumb", b"x")
    write(root, "Otros/b.txt", b"1")
    remote.put("Otros/c.txt", b"1")
    make(folders=["Docs"]).run()
    assert set(remote.files) == {"Docs/a.txt", "Otros/c.txt"}
    assert not (root / "Otros/c.txt").exists()


def test_auth_error_aborts_run(env):
    root, remote, db, make, _ = env
    remote.auth_fail = True
    with pytest.raises(AuthRequired):
        make().run()


def test_nfd_local_names_match_nfc_remote(env):
    root, remote, db, make, _ = env
    import unicodedata
    name = "canción.txt"
    write(root, unicodedata.normalize("NFD", name), b"x", mtime=1_700_000_000)
    remote.put(unicodedata.normalize("NFC", name), b"x", mtime=1_700_000_000)
    assert make().run().counts == {"adopt": 1}


def test_leftover_tmp_download_is_cleaned(env):
    root, remote, db, make, _ = env
    write(root, "docs/.icloud-sync-tmp-a.pdf", b"a medias")
    write(root, "docs/b.pdf", b"b")
    make().run()
    assert not (root / "docs/.icloud-sync-tmp-a.pdf").exists()
    assert set(remote.files) == {"docs/b.pdf"}


def test_unfinished_run_is_marked_interrupted_on_startup(tmp_path):
    db = StateDB(tmp_path / "s.db")
    db.start_run(False)
    runs = StateDB(tmp_path / "s.db").recent_runs()
    assert runs[0]["status"] == "interrupted" and runs[0]["finished_at"]


def test_names_differing_only_in_case_never_trigger_deletions(env):
    # iCloud admite "x.jpg" y "x.JPG" en la misma carpeta: antes uno se ignoraba en local y
    # parecía borrado, y se mandaba a Eliminados de iCloud (pasó con 6 fotos en producción).
    root, remote, db, make, _ = env
    remote.put("Kika/IMG_8561.jpg", b"minus")
    remote.put("Kika/IMG_8561.JPG", b"MAYUS")
    make().run()
    assert (root / "Kika/IMG_8561.jpg").read_bytes() == b"minus"
    assert (root / "Kika/IMG_8561.JPG").read_bytes() == b"MAYUS"
    res = make().run()
    assert res.counts == {} and remote.trashed == []


def test_path_ignored_on_one_side_is_not_treated_as_deleted(env):
    root, remote, db, make, _ = env
    write(root, "x.txt", b"1")
    make().run()
    (root / "x.txt").unlink()
    (root / "x.txt").symlink_to("/etc/hostname")   # el escaneo local ya no puede usarlo
    res = make().run()
    assert res.counts == {} and remote.trashed == [] and "x.txt" in remote.files
    assert any("x.txt" in w for w in res.warnings)
    assert "x.txt" in db.load_snapshot()           # el snapshot se conserva para cuando vuelva


def _rename_remote_dir(remote, old, new):
    for p in [p for p in remote.files if p == old or p.startswith(old + "/")]:
        remote.files[new + p[len(old):]] = remote.files.pop(p)
    for d in [d for d in remote.dirs if d == old or d.startswith(old + "/")]:
        remote.dirs.discard(d)
        remote.dirs.add(new + d[len(old):])


def test_folder_renamed_in_icloud_is_renamed_on_nas(env):
    # Caso real: "Minha Senhora playa " (con espacio final) apareció en iCloud como "IMXQRH~J"
    root, remote, db, make, tmp = env
    for i in range(5):
        remote.put(f"Sesiones/Minha Senhora playa /IMG_{i}.heic", b"x" * (i + 1))
    remote.put("Sesiones/Minha Senhora playa /sub/a.jpg", b"a")
    remote.put("otra.txt", b"o")
    make().run()
    _rename_remote_dir(remote, "Sesiones/Minha Senhora playa ", "Sesiones/IMXQRH~J")
    res = make().run()
    assert res.counts == {"rename_local": 1}, res.counts
    assert (root / "Sesiones/IMXQRH~J/IMG_0.heic").exists()
    assert (root / "Sesiones/IMXQRH~J/sub/a.jpg").exists()
    assert not (root / "Sesiones/Minha Senhora playa ").exists()
    assert not list((tmp / "trash").rglob("*")) if (tmp / "trash").exists() else True
    assert make().run().counts == {}                      # snapshot al día
    # y al devolverle el nombre bueno (sin espacio) en iCloud, el NAS lo sigue
    _rename_remote_dir(remote, "Sesiones/IMXQRH~J", "Sesiones/Minha Senhora playa")
    assert make().run().counts == {"rename_local": 1}
    assert (root / "Sesiones/Minha Senhora playa/IMG_4.heic").exists()


def test_rename_plus_new_file_downloads_only_the_new_one(env):
    root, remote, db, make, _ = env
    for i in range(4):
        remote.put(f"A/f{i}.txt", b"1")
    remote.put("queda.txt", b"q")
    make().run()
    _rename_remote_dir(remote, "A", "B")
    remote.put("B/nuevo.txt", b"n")
    res = make().run()
    assert res.counts == {"rename_local": 1, "download": 1}


def test_ambiguous_rename_falls_back_safely(env):
    # dos carpetas nuevas con el mismo contenido: no se adivina, se hace lo de siempre (sin pérdidas)
    root, remote, db, make, tmp = env
    remote.put("A/f.txt", b"1")
    remote.put("queda.txt", b"q")
    make().run()
    _rename_remote_dir(remote, "A", "B")
    remote.put("C/f.txt", b"1")
    res = make().run()
    assert "rename_local" not in res.counts
    assert (root / "B/f.txt").exists() and (root / "C/f.txt").exists()
    assert list((tmp / "trash").rglob("f.txt"))           # la vieja, a la papelera


def test_dry_run_reports_rename_without_touching(env):
    root, remote, db, make, _ = env
    remote.put("A/f.txt", b"1")
    remote.put("queda.txt", b"q")
    make().run()
    _rename_remote_dir(remote, "A", "B~X")
    res = make().run(dry_run=True)
    assert res.counts == {"rename_local": 1}
    assert (root / "A/f.txt").exists() and not (root / "B~X").exists()


def _write_renames(tmp, pairs):
    import json
    (tmp / "RENOMBRAR.json").write_text(json.dumps({"renombrar": [{"de": a, "a": b} for a, b in pairs]}))


def test_requested_rename_on_both_sides_without_reupload(env):
    root, remote, db, make, tmp = env
    for i in range(3):
        remote.put(f"Personal/Ojanco /f{i}.jpg", b"x" * (i + 1))
    remote.put("Personal/Twitter /t.jpg", b"t")
    make().run()
    _write_renames(tmp, [("Personal/Ojanco ", "Personal/Ojanco"), ("Personal/Twitter ", "Personal/Twitter")])
    res = make().run()
    assert res.counts == {"rename_both": 2}, res.counts
    assert remote.renamed == [("Personal/Ojanco ", "Personal/Ojanco"), ("Personal/Twitter ", "Personal/Twitter")]
    assert (root / "Personal/Ojanco/f2.jpg").exists() and not (root / "Personal/Ojanco ").exists()
    assert "Personal/Ojanco/f0.jpg" in remote.files and remote.trashed == []
    assert not (tmp / "RENOMBRAR.json").exists() and (tmp / "RENOMBRAR.hecho.json").exists()
    assert make().run().counts == {}


def test_requested_rename_dry_run_changes_nothing(env):
    root, remote, db, make, tmp = env
    remote.put("A /f.jpg", b"x")
    make().run()
    _write_renames(tmp, [("A ", "A")])
    res = make().run(dry_run=True)
    assert res.counts == {"rename_both": 1}
    assert remote.renamed == [] and (root / "A /f.jpg").exists() and (tmp / "RENOMBRAR.json").exists()


def test_requested_rename_refuses_when_target_exists(env):
    root, remote, db, make, tmp = env
    remote.put("A /f.jpg", b"x")
    remote.put("A/g.jpg", b"y")
    make().run()
    _write_renames(tmp, [("A ", "A")])
    res = make().run()
    assert "rename_both" not in res.counts and remote.renamed == []
    assert any("ya existe" in w for w in res.warnings)
    assert (tmp / "RENOMBRAR.json").exists()          # se queda para revisarlo


def test_per_folder_decisions_and_rest_stays_pending_below_limit(env):
    root, remote, db, make, _ = env
    for i in range(5):
        remote.put(f"grande/f{i}.txt", b"x")
    for i in range(2):
        remote.put(f"chica/g{i}.txt", b"y")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "grande")
    shutil.rmtree(root / "chica")
    make(max_delete=3).run()
    batch = db.pending_items()[0]["batch_id"]
    assert make(max_delete=3).run() and db.pending_items()[0]["batch_id"] == batch   # id estable
    grande = [(i["kind"], i["path"]) for i in db.pending_items() if i["path"].startswith("grande")]
    assert db.decide_paths(batch, grande, "confirmed") == 6
    res = make(max_delete=3).run()
    assert not any(p.startswith("grande/") for p in remote.files)              # confirmado: borrado
    # lo que queda (2 ficheros + carpeta = 3, ya por debajo del límite) NO se borra solo
    assert all(f"chica/g{i}.txt" in remote.files for i in range(2))
    assert res.status == "pending" and {i["path"].split("/")[0] for i in db.pending_items()} == {"chica"}
    chica = [(i["kind"], i["path"]) for i in db.pending_items()]
    db.decide_paths(db.pending_items()[0]["batch_id"], chica, "rejected")
    res = make(max_delete=3).run()
    assert res.status == "ok" and (root / "chica/g0.txt").exists() and db.pending_items() == []


# --- renombrados y movimientos en los dos sentidos -------------------------------------------------

def _base(env):
    root, remote, db, make, tmp = env
    for i in range(3):
        remote.put(f"A/f{i}.jpg", b"x" * (i + 10), mtime=1_700_000_000 + i)
    remote.put("X/otro.txt", b"o")
    remote.put("suelto.pdf", b"PDF" * 50, mtime=1_700_000_500)
    make().run()
    return root, remote, db, make, tmp


def test_folder_renamed_on_nas_is_renamed_in_icloud(env):
    root, remote, db, make, tmp = _base(env)
    os.rename(root / "A", root / "B")
    res = make(confirm_over=0).run()
    assert res.counts == {"rename_remote": 1}, res.counts
    assert remote.moved == [("A", "B")] and "B/f2.jpg" in remote.files and remote.trashed == []
    assert make(confirm_over=0).run().counts == {}


def test_folder_moved_into_other_folder_on_nas(env):
    root, remote, db, make, tmp = _base(env)
    os.rename(root / "A", root / "X/A")
    res = make(confirm_over=0).run()
    assert res.counts == {"rename_remote": 1} and remote.moved == [("A", "X/A")]
    assert "X/A/f0.jpg" in remote.files and make(confirm_over=0).run().counts == {}


def test_file_moved_on_nas_keeps_name_and_is_moved_in_icloud(env):
    root, remote, db, make, tmp = _base(env)
    os.rename(root / "suelto.pdf", root / "X/suelto.pdf")
    res = make(confirm_over=0).run()
    assert res.counts == {"rename_remote": 1} and remote.moved == [("suelto.pdf", "X/suelto.pdf")]
    assert make(confirm_over=0).run().counts == {}


def test_file_renamed_on_nas_uploads_new_and_deletes_old_without_confirmation(env):
    root, remote, db, make, tmp = _base(env)
    for i in range(3):
        os.rename(root / f"A/f{i}.jpg", root / f"A/foto {i}.jpg")
    res = make(confirm_over=0).run()                     # umbral 0: cualquier borrado real pediría confirmación
    assert res.status == "ok" and res.counts == {"upload": 3, "delete_remote": 3}
    assert all(f"A/foto {i}.jpg" in remote.files for i in range(3)) and db.pending_items() == []


def test_file_renamed_in_icloud_is_renamed_on_nas(env):
    root, remote, db, make, tmp = _base(env)
    remote.files["X/nuevo nombre.pdf"] = remote.files.pop("suelto.pdf")
    res = make(confirm_over=0).run()
    assert res.counts == {"rename_local": 1}
    assert (root / "X/nuevo nombre.pdf").read_bytes() == b"PDF" * 50 and not (root / "suelto.pdf").exists()
    assert make(confirm_over=0).run().counts == {}


def test_ambiguous_file_renames_are_not_paired(env):
    root, remote, db, make, tmp = env
    remote.put("a1.txt", b"igual", mtime=1_700_000_000)
    remote.put("a2.txt", b"igual", mtime=1_700_000_000)
    remote.put("queda.txt", b"q")
    make().run()
    os.rename(root / "a1.txt", root / "c1.txt")
    os.rename(root / "a2.txt", root / "c2.txt")
    res = make().run()
    assert "rename_remote" not in res.counts and remote.moved == []
    assert res.counts == {"upload": 2, "delete_remote": 2}      # lo de siempre, sin pérdidas


def test_zero_byte_files_are_never_paired(env):
    root, remote, db, make, tmp = env
    remote.put("vacio.txt", b"", mtime=1_700_000_000)
    remote.put("queda.txt", b"q")
    make().run()
    os.rename(root / "vacio.txt", root / "otro.txt")
    assert "rename_remote" not in make().run().counts


def test_confirmation_threshold_counts_files_only(env):
    root, remote, db, make, tmp = env
    for i in range(5):
        remote.put(f"cinco/f{i}.txt", b"x")
    for i in range(6):
        remote.put(f"seis/g{i}.txt", b"y")
    remote.put("queda.txt", b"q")
    make().run()
    import shutil
    shutil.rmtree(root / "cinco")                        # 5 ficheros + su carpeta: se hace solo
    res = make(confirm_over=5).run()
    assert res.status == "ok" and res.counts == {"delete_remote": 5, "rmdir_remote": 1}
    shutil.rmtree(root / "seis")                         # 6 ficheros: pide confirmación
    res = make(confirm_over=5).run()
    assert res.status == "pending" and all(f"seis/g{i}.txt" in remote.files for i in range(6))
