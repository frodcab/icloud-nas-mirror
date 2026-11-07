# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Sonda de iCloud Fotos contra una fototeca falsa con la forma de pyicloud 2.7.0."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from services.photos_probe import PhotosProbe


class FakeRes:
    def __init__(self, size, type_):
        self.size, self.type = size, type_


class FakeAsset:
    def __init__(self, name, live=False, edited=False, movie=False):
        self.filename = name
        self.item_type = "movie" if movie else "image"
        self.is_live_photo = live
        self.asset_date = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.size = 3 * 2**20
        self.resources = {"original": FakeRes(self.size, "public.heic"), "medium": FakeRes(2**18, "public.jpeg")}
        if live:
            self.resources["original_video"] = FakeRes(2**21, "com.apple.quicktime-movie")
        fields = {"adjustmentType": {"value": "com.apple.photo"},
                  "resJPEGFullRes": {"value": {"size": 4 * 2**20, "downloadURL": "https://secreto"}}} if edited else {}
        self._asset_record = {"fields": fields}
        self._master_record = {"fields": {}}
        self.deleted = False

    def delete(self):
        self.deleted = True
        return True


class FakeAlbum:
    def __init__(self, name, assets):
        self.name, self._assets = name, assets

    def __len__(self):
        return len(self._assets)

    @property
    def photos(self):
        yield from self._assets


class FakeClient:
    def __init__(self):
        self._uploader = object()
        self.calls = 0

    def upload_file(self, path, zone_name):
        self.calls += 1
        return SimpleNamespace(is_duplicate=self.calls > 1, cplAsset="A1", cplMaster="M1")


class FakeLibrary:
    zone_id = {"zoneName": "PrimarySync"}

    def __init__(self):
        assets = [FakeAsset("IMG_1.HEIC", live=True), FakeAsset("IMG_2.HEIC", edited=True),
                  FakeAsset("IMG_3.MOV", movie=True)]
        self.all = FakeAlbum("Todo", assets)
        self.albums = [self.all, FakeAlbum("Viaje", assets[:1])]
        self._client = FakeClient()
        self.uploaded = FakeAsset("prueba.jpg")

    def sync_cursor(self):
        return "token"

    def iter_changes(self, since=None):
        for _ in range(100):
            yield SimpleNamespace(kind="updated", record_type="CPLAsset")

    def _hydrate_uploaded_asset(self, result):
        return self.uploaded


def _probe(tmp_path, library):
    api = SimpleNamespace(photos=SimpleNamespace(libraries={"root": library}))
    auth = SimpleNamespace(require_api=lambda: api)
    return PhotosProbe(auth, tmp_path, tmp_path / "PRUEBA_FOTOS")


def test_lectura_sin_urls_y_una_vez(tmp_path):
    lib = FakeLibrary()
    probe = _probe(tmp_path, lib)
    probe.run()
    out = (tmp_path / "photos_probe.json").read_text()
    assert "https://secreto" not in out          # las URLs firmadas no se guardan
    r = json.loads(out)["lectura"]
    assert r["total_fotos"] == 3 and r["albumes"] == {"Todo": 3, "Viaje": 1}
    assert r["muestra"]["live"] == 1 and r["muestra"]["editadas"] == 1 and r["muestra"]["edicion_en_asset"] == 1
    assert r["cambios_muestra"] == {"updated:CPLAsset": 50}
    assert "escritura" not in json.loads(out) and lib._client.calls == 0
    probe.run()                                  # el segundo run no vuelve a leer
    assert lib._client.calls == 0


def test_escritura_solo_con_fichero(tmp_path):
    lib = FakeLibrary()
    (tmp_path / "PRUEBA_FOTOS").write_text("")
    _probe(tmp_path, lib).run()
    w = json.loads((tmp_path / "photos_probe.json").read_text())["escritura"]
    assert w["subida"]["duplicada"] is False and w["resubida"] == {"duplicada": True, "mismo_asset": True}
    assert w["borrado"] is True and lib.uploaded.deleted
    assert not (tmp_path / "PRUEBA_FOTOS").exists() and (tmp_path / "PRUEBA_FOTOS.hecho").exists()


def test_fallo_no_propaga(tmp_path):
    def boom():
        raise RuntimeError("sin sesión")
    PhotosProbe(SimpleNamespace(require_api=boom), tmp_path, tmp_path / "PRUEBA_FOTOS").run()
