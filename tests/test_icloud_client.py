# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""ICloudDriveRemote + SyncEngine contra nodos falsos con la misma forma que pyicloud.services.drive.DriveNode.

No sustituye a la prueba contra la cuenta real (ver README), pero cubre el mapeo nodo -> ruta,
la lectura de etags tras subir, el reemplazo vía papelera y los errores de sesión.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyicloud.exceptions import PyiCloudAPIResponseException  # noqa: E402

from services.entries import AuthRequired  # noqa: E402
from services.icloud_client import ICloudDriveRemote  # noqa: E402
from services.local_fs import LocalTree  # noqa: E402
from services.state_db import StateDB  # noqa: E402
from services.sync_engine import SyncEngine  # noqa: E402


class FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class FakeNode:
    _seq = 0

    def __init__(self, name, kind, parent=None, data=b"", mtime=None):
        FakeNode._seq += 1
        stem, dot, ext = name.rpartition(".")
        self.data = {
            "name": stem if (dot and kind == "FILE") else name,
            "type": kind, "etag": f"t{FakeNode._seq}",
            "drivewsid": f"{kind}::{FakeNode._seq}", "docwsid": str(FakeNode._seq), "zone": "com.apple.CloudDocs",
        }
        if dot and kind == "FILE":
            self.data["extension"] = ext
        self.parent = parent
        self.children: list[FakeNode] = []
        self.content = data
        self.mtime = mtime or datetime(2026, 1, 1)
        self.fail_auth = False

    # superficie de DriveNode usada por ICloudDriveRemote
    @property
    def name(self):
        return f"{self.data['name']}.{self.data['extension']}" if "extension" in self.data else self.data["name"]

    @property
    def type(self):
        return self.data["type"].lower()

    @property
    def size(self):
        return len(self.content) if self.type == "file" else None

    @property
    def date_modified(self):
        return self.mtime if self.type == "file" else None

    def get_children(self, force=False):
        if self.fail_auth:
            raise PyiCloudAPIResponseException("Unauthorized", 421)
        return list(self.children)

    def open(self, stream=False):
        return FakeResponse(self.content)

    def upload(self, f, mtime=None, ctime=None):
        assert "/" not in f.name, "pyicloud usa file_object.name tal cual como nombre en iCloud"
        f.seek(0)
        self.children.append(FakeNode(f.name, "FILE", self, f.read(), datetime.utcfromtimestamp(mtime)))

    def mkdir(self, name):
        self.children.append(FakeNode(name, "FOLDER", self))

    def rename(self, name):
        self.data["name"] = name

    def move_to_trash(self):
        self.parent.children.remove(self)

    def add(self, name, kind="FILE", data=b""):
        node = FakeNode(name, kind, self, data)
        self.children.append(node)
        return node


class FakeDrive:
    def __init__(self, root):
        self.root = root

    def refresh_root(self):
        pass


class FakeAuth:
    def __init__(self, root):
        self.api = type("Api", (), {"drive": FakeDrive(root)})()
        self.state = "ok"
        self.expired = None

    def require_api(self):
        if self.state != "ok":
            raise AuthRequired(self.state)
        return self.api

    def mark_expired(self, reason):
        self.state, self.expired = "disconnected", reason


@pytest.fixture
def env(tmp_path):
    root_node = FakeNode("root", "FOLDER")
    auth = FakeAuth(root_node)
    remote = ICloudDriveRemote(auth, retries=1)
    local_root = tmp_path / "drive"
    local_root.mkdir()
    db = StateDB(tmp_path / "s.db")
    engine = SyncEngine(db, LocalTree(local_root, tmp_path / "trash"), remote, [".DS_Store"], [], 100)
    return root_node, auth, local_root, engine


def test_roundtrip_through_pyicloud_shaped_nodes(env):
    root, auth, local_root, engine = env
    docs = root.add("Documentos", "FOLDER")
    docs.add("informe.pdf", data=b"PDF")
    root.add("Pages", "APP_LIBRARY")
    (local_root / "Nuevo").mkdir()
    (local_root / "Nuevo" / "nota.txt").write_bytes(b"hola")

    res = engine.run()
    assert res.status == "ok", res.errors
    assert (local_root / "Documentos" / "informe.pdf").read_bytes() == b"PDF"
    nuevo = next(c for c in root.children if c.name == "Nuevo")
    assert [c.name for c in nuevo.children] == ["nota.txt"] and nuevo.children[0].content == b"hola"
    assert engine.run().counts == {}  # el etag leído tras subir evita re-subir/re-bajar

    # reemplazo: la versión vieja va a la papelera y la nueva se sube con el mismo nombre
    import os
    p = local_root / "Nuevo" / "nota.txt"
    p.write_bytes(b"hola 2")
    os.utime(p, (p.stat().st_mtime + 10,) * 2)
    assert engine.run().counts == {"replace": 1}
    assert [c.content for c in nuevo.children] == [b"hola 2"]


def test_session_expired_mid_run_raises_auth_required(env):
    root, auth, local_root, engine = env
    root.fail_auth = True
    with pytest.raises(AuthRequired):
        engine.run()
    assert auth.state == "disconnected" and "421" in auth.expired


def _big_tree():
    root = FakeNode("root", "FOLDER")
    for a in range(5):
        fa = root.add(f"A{a}", "FOLDER")
        fa.add(f"f{a}.txt", data=b"x" * a)
        for b in range(4):
            fb = fa.add(f"B{b}", "FOLDER")
            for c in range(3):
                fb.add(f"doc {c}.pdf", data=b"y" * c)
    root.add("Pages", "APP_LIBRARY")
    return root


def test_parallel_listing_matches_sequential():
    results = []
    for workers in (1, 4):
        remote = ICloudDriveRemote(FakeAuth(_big_tree()), retries=1, list_workers=workers)
        entries, warnings = remote.list_tree([".DS_Store"], [])
        results.append(({p: (e.is_dir, e.size) for p, e in entries.items()}, warnings, set(remote._nodes)))
    assert results[0] == results[1]
    assert len(results[0][0]) == 5 + 5 + 20 + 60


def test_parallel_listing_propagates_auth_error():
    root = _big_tree()
    root.children[2].children[1].fail_auth = True   # una subcarpeta caduca a mitad del recorrido
    auth = FakeAuth(root)
    with pytest.raises(AuthRequired):
        ICloudDriveRemote(auth, retries=1, list_workers=4).list_tree([], [])
    assert auth.state == "disconnected"


def test_session_saves_are_serialized():
    import threading
    import time

    class FakeSession:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        def _save_session_data(self):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            self.active -= 1

    auth = FakeAuth(_big_tree())
    auth.api.session = FakeSession()
    remote = ICloudDriveRemote(auth, retries=1, list_workers=4)
    remote._serialize_session_saves()
    remote._serialize_session_saves()   # idempotente: no se envuelve dos veces
    threads = [threading.Thread(target=auth.api.session._save_session_data) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert auth.api.session.max_active == 1


def test_upload_reader_is_a_real_file_for_python_312_plus(tmp_path):
    # requests usa isinstance(fp, _SupportsRead), que desde Python 3.12 mira con getattr_static
    import inspect
    from services.icloud_client import _NamedReader
    p = tmp_path / "a.jpg"
    p.write_bytes(b"abc")
    with _NamedReader(p, "a.jpg") as f:
        for attr in ("read", "seek", "tell"):
            inspect.getattr_static(f, attr)          # AttributeError si fuera vía __getattr__
        assert f.read() == b"abc" and f.name == "a.jpg"


def test_download_accepts_file_that_changed_since_listing(tmp_path):
    root = FakeNode("root", "FOLDER")
    node = root.add("foto.jpg", data=b"vieja")
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    entry = entries["foto.jpg"]
    # cambia en iCloud entre el listado y la descarga
    node.content = b"version nueva y mas larga"
    node.data["etag"] = "nuevo"
    remote.download(entry, tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"version nueva y mas larga"
    assert entry.size == len(b"version nueva y mas larga") and entry.etag == "nuevo"


def test_download_size_mismatch_still_fails_if_it_does_not_add_up(tmp_path):
    root = FakeNode("root", "FOLDER")
    node = root.add("foto.jpg", data=b"12345")
    node.open = lambda stream=False: FakeResponse(b"12")   # la conexión se corta a medias
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    entry = entries["foto.jpg"]
    with pytest.raises(IOError, match="no cuadra"):
        remote.download(entry, tmp_path / "out")


def test_app_libraries_are_ignored_on_both_sides(tmp_path):
    root = FakeNode("root", "FOLDER")
    root.add("Pages", "APP_LIBRARY")
    root.add("doc.txt", data=b"x")
    local_root = tmp_path / "drive"
    (local_root / "Pages").mkdir(parents=True)
    (local_root / "Pages" / "local.pages").write_bytes(b"p")
    engine = SyncEngine(StateDB(tmp_path / "s.db"), LocalTree(local_root, tmp_path / "t"),
                        ICloudDriveRemote(FakeAuth(root), retries=1), [], [], 100)
    res = engine.run()
    # ni se sube la carpeta local "Pages" (chocaría con la App Library) ni se toca nada de ella
    assert res.counts == {"download": 1}



class _PkgResponse:
    def __init__(self, data=b"", payload=None):
        self._data, self._payload = data, payload

    def json(self):
        return self._payload

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class _PkgConn:
    """Imita DriveService para _open(): download/by_id devuelve package_token o data_token."""
    _document_root = "https://docws"
    params = {"clientId": "x"}

    def __init__(self, token_kind, body):
        self.token_kind, self.body = token_kind, body
        self.session = self

    def get(self, url, params=None, stream=False):
        if url.endswith("/download/by_id"):
            return _PkgResponse(payload={self.token_kind: {"url": "https://content/blob"}})
        return _PkgResponse(self.body)


def _zip_bytes():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Memoria.pages/Index.zip", b"x" * 500)
        z.writestr("Memoria.pages/preview.jpg", b"y" * 500)
    return buf.getvalue()


def test_package_download_is_validated_as_zip_not_by_size(tmp_path, caplog):
    import logging
    root = FakeNode("root", "FOLDER")
    node = root.add("Memoria.pages", data=b"z" * 5000)     # iCloud anuncia el tamaño sin comprimir
    body = _zip_bytes()
    node.connection = _PkgConn("package_token", body)
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    with caplog.at_level(logging.INFO):
        remote.download(entries["Memoria.pages"], tmp_path / "out")
    import zipfile
    with zipfile.ZipFile(tmp_path / "out") as z:          # archivo único: contenido en la raíz
        assert sorted(z.namelist()) == ["Index.zip", "preview.jpg"]
        assert z.read("Index.zip") == b"x" * 500
    assert any("single iWork file" in r.message and "Memoria.pages" in r.message for r in caplog.records)


def test_package_download_that_is_not_a_zip_fails(tmp_path):
    root = FakeNode("root", "FOLDER")
    node = root.add("Roto.pages", data=b"z" * 5000)
    node.connection = _PkgConn("package_token", b"esto no es un zip")
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    with pytest.raises(IOError, match="zip"):
        remote.download(entries["Roto.pages"], tmp_path / "out")


def test_data_token_download_still_checks_size(tmp_path):
    root = FakeNode("root", "FOLDER")
    node = root.add("foto.jpg", data=b"12345")
    node.connection = _PkgConn("data_token", b"12")          # llega cortado
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    with pytest.raises(IOError, match="no cuadra"):
        remote.download(entries["foto.jpg"], tmp_path / "out")


def test_flatten_handles_nfd_names_from_apple(tmp_path):
    import unicodedata
    import zipfile
    from services.packages import flatten_package_zip
    doc = "Lista de comprobación.numbers"
    p = tmp_path / doc
    with zipfile.ZipFile(p, "w") as z:
        nfd = unicodedata.normalize("NFD", doc)
        z.writestr(f"{nfd}/", b"")
        z.writestr(f"{nfd}/Index.zip", b"i")
        z.writestr(f"{nfd}/Metadata/Properties.plist", b"m")
    assert flatten_package_zip(p, doc)
    assert sorted(zipfile.ZipFile(p).namelist()) == ["Index.zip", "Metadata/Properties.plist"]


def test_rename_dir_renames_and_reindexes_nodes():
    root = FakeNode("root", "FOLDER")
    personal = root.add("Personal", "FOLDER")
    ojanco = personal.add("Ojanco ", "FOLDER")
    ojanco.add("f.jpg", data=b"x")
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    remote.list_tree([], [])
    remote.rename_dir("Personal/Ojanco ", "Personal/Ojanco")
    assert ojanco.name == "Ojanco"
    assert "Personal/Ojanco/f.jpg" in remote._nodes and "Personal/Ojanco /f.jpg" not in remote._nodes
    with pytest.raises(ValueError):
        remote.rename_dir("Personal/Ojanco", "Otra/Ojanco")


class _MoveConn:
    """Imita DriveService.move_nodes_to_node sobre FakeNode."""
    def move_nodes_to_node(self, nodes, dest):
        for n in nodes:
            n.parent.children.remove(n)
            n.parent = dest
            dest.children.append(n)


def test_move_to_other_folder_keeping_name():
    root = FakeNode("root", "FOLDER")
    a = root.add("A", "FOLDER")
    x = root.add("X", "FOLDER")
    f = a.add("foto.jpg", data=b"x")
    f.connection = _MoveConn()
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    remote.list_tree([], [])
    remote.move("A/foto.jpg", "X/foto.jpg")
    assert [c.name for c in x.children] == ["foto.jpg"] and a.children == []
    assert "X/foto.jpg" in remote._nodes and "A/foto.jpg" not in remote._nodes


def test_trash_folder_and_file_through_real_client():
    # Faltaba: el cliente real nunca se probaba borrando una carpeta en iCloud (NameError en prod).
    root = FakeNode("root", "FOLDER")
    dieta = root.add("Dieta", "FOLDER")
    f = dieta.add("a.pdf", data=b"x")
    remote = ICloudDriveRemote(FakeAuth(root), retries=1)
    entries, _ = remote.list_tree([], [])
    remote.trash(entries["Dieta/a.pdf"])
    remote.trash(entries["Dieta"])
    assert root.children == [] and dieta.children == []


def test_folder_deleted_on_nas_is_removed_in_icloud_end_to_end(tmp_path):
    root = FakeNode("root", "FOLDER")
    dieta = root.add("Dieta", "FOLDER")
    for i in range(2):
        dieta.add(f"d{i}.pdf", data=b"x" * (i + 1))
    root.add("queda.txt", data=b"q")
    local_root = tmp_path / "drive"
    local_root.mkdir()
    engine = SyncEngine(StateDB(tmp_path / "s.db"), LocalTree(local_root, tmp_path / "t"),
                        ICloudDriveRemote(FakeAuth(root), retries=1), [], [], 100)
    engine.run()
    import shutil
    shutil.rmtree(local_root / "Dieta")
    res = engine.run()
    assert res.status == "ok", res.errors
    assert res.counts == {"delete_remote": 2, "rmdir_remote": 1}
    assert [c.name for c in root.children] == ["queda.txt"]
