# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Copia de las App Libraries: solo iCloud → NAS, con papelera y salvaguardas."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_icloud_client import FakeAuth, FakeNode  # noqa: E402

from services.apps_mirror import AppsMirror  # noqa: E402
from services.entries import SyncAborted  # noqa: E402
from services.icloud_client import ICloudDriveRemote  # noqa: E402
from services.local_fs import LocalTree  # noqa: E402
from services.state_db import StateDB  # noqa: E402
from services.sync_engine import SyncEngine  # noqa: E402


@pytest.fixture
def env(tmp_path):
    root = FakeNode("root", "FOLDER")
    root.add("doc.txt", data=b"drive")
    pages = root.add("Pages", "APP_LIBRARY")
    pages.add("Memoria.pages", data=b"PAGES-1")
    carpeta = pages.add("Pfc", "FOLDER")
    carpeta.add("cap1.pages", data=b"cap1")
    numbers = root.add("Numbers", "APP_LIBRARY")
    numbers.add("Natación .numbers", data=b"N" * 10)
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    drive, apps_dir, trash = tmp_path / "drive", tmp_path / "apps", tmp_path / "trash"
    drive.mkdir()
    engine = SyncEngine(StateDB(tmp_path / "s.db"), LocalTree(drive, trash), remote, [], [], 100)
    mirror = AppsMirror(StateDB(tmp_path / "apps.db"), LocalTree(apps_dir, trash, trash_prefix="Apps"),
                        remote, [], 100)

    def run(max_delete=None):
        engine.run()                       # la sync principal es la que descubre las App Libraries
        if max_delete is not None:
            mirror.max_delete = max_delete
        return mirror.run()

    return root, pages, apps_dir, drive, trash, run


def test_first_copy_and_nothing_goes_to_drive(env):
    root, pages, apps_dir, drive, trash, run = env
    res = run()
    assert res.status == "ok", res.errors
    assert (apps_dir / "Pages/Memoria.pages").read_bytes() == b"PAGES-1"
    assert (apps_dir / "Pages/Pfc/cap1.pages").read_bytes() == b"cap1"
    assert (apps_dir / "Numbers/Natación .numbers").exists()
    assert not (drive / "Pages").exists()                     # la sync bidireccional no las toca
    assert [c.name for c in root.children] == ["doc.txt", "Pages", "Numbers"]   # iCloud intacto
    assert run().counts == {}


def test_changes_in_icloud_are_mirrored_with_trash(env):
    root, pages, apps_dir, drive, trash, run = env
    run()
    memoria = pages.children[0]
    memoria.content, memoria.data["etag"] = b"PAGES-2 mas largo", "v2"
    pages.children.remove(pages.children[1])                  # la carpeta Pfc se borra en la app
    res = run()
    assert res.counts == {"apps_replace": 1, "apps_trash": 1, "apps_rmdir": 1}
    assert (apps_dir / "Pages/Memoria.pages").read_bytes() == b"PAGES-2 mas largo"
    assert not (apps_dir / "Pages/Pfc").exists()
    trashed = {p.name for p in trash.rglob("*") if p.is_file()}
    assert {"Memoria.pages", "cap1.pages"} <= trashed           # la versión vieja y lo retirado
    assert all("Apps" in str(p) for p in trash.rglob("*.pages"))


def test_local_edits_are_reverted_to_icloud(env):
    root, pages, apps_dir, drive, trash, run = env
    run()
    f = apps_dir / "Pages/Memoria.pages"
    f.write_bytes(b"editado en el NAS")
    os.utime(f, (f.stat().st_mtime + 30,) * 2)
    (apps_dir / "Pages/extra.txt").write_bytes(b"x")
    res = run()
    assert res.counts == {"apps_replace": 1, "apps_trash": 1}
    assert f.read_bytes() == b"PAGES-1"
    assert not (apps_dir / "Pages/extra.txt").exists()
    assert any(p.read_bytes() == b"editado en el NAS" for p in trash.rglob("Memoria.pages"))


def test_app_returned_empty_is_left_alone(env):
    root, pages, apps_dir, drive, trash, run = env
    run()
    pages.children.clear()                                    # respuesta vacía de Apple
    res = run()
    assert (apps_dir / "Pages/Memoria.pages").exists() and res.counts == {}
    assert any("vacía" in w for w in res.warnings)


def test_max_delete_aborts(env):
    root, pages, apps_dir, drive, trash, run = env
    run()
    numbers = root.children[2]
    for i in range(3):
        (apps_dir / f"Numbers/extra{i}.txt").write_bytes(b"x")
    with pytest.raises(SyncAborted):
        run(max_delete=2)
    assert (apps_dir / "Numbers/extra0.txt").exists()


def test_packages_copied_before_the_fix_are_flattened_once(env, tmp_path):
    import zipfile
    from services.packages import wrapped_folder
    root, pages, apps_dir, drive, trash, run = env
    run()
    # simula un .pages copiado con la versión anterior: zip con la carpeta dentro y tamaño distinto
    f = apps_dir / "Pages/Memoria.pages"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Memoria.pages/Index.zip", b"i" * 100)
        z.writestr("Memoria.pages/Data/foto.jpg", b"d" * 100)
    mirror_db = StateDB(tmp_path / "apps.db")
    snap = mirror_db.load_snapshot()["Pages/Memoria.pages"]
    st = f.stat()
    snap.l_size, snap.l_mtime_ns, snap.r_size = st.st_size, st.st_mtime_ns, st.st_size + 999
    mirror_db.put(snap)
    mirror_db.set("package_format", None)
    res = run()
    with zipfile.ZipFile(f) as z:
        assert sorted(z.namelist()) == ["Data/foto.jpg", "Index.zip"]
        assert wrapped_folder(z.namelist(), "Memoria.pages") is None
    assert res.counts == {}                     # no se re-descarga ni se da por "tocado en el NAS"
    assert mirror_db.get("package_format") == "flat"
