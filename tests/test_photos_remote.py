# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Lado iCloud de la sync de Fotos: reintentos, frenazos de Apple, descarga por trozos y paginado."""

import time
from types import SimpleNamespace

import pytest
from pyicloud.exceptions import PyiCloudAPIResponseException

from services.entries import AuthRequired
from services.photos_remote import ICloudPhotosRemote, RemoteFile, Throttled


class FakeAuth:
    def __init__(self, api=None):
        self.api, self.expired = api, None

    def require_api(self):
        return self.api

    def mark_expired(self, reason):
        self.expired = reason


def _remote(api=None):
    return ICloudPhotosRemote(FakeAuth(api), sleep=lambda s: None)


def test_frenazo_tras_los_reintentos():
    r = _remote()
    calls = []

    def boom():
        calls.append(1)
        raise PyiCloudAPIResponseException("busy", 503)
    with pytest.raises(Throttled):
        r._call(boom)
    assert len(calls) == 3


def test_frenazo_puntual_se_reintenta():
    r = _remote()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise PyiCloudAPIResponseException("busy", 429)
        return "ok"
    assert r._call(flaky) == "ok"


def test_sesion_caducada():
    r = _remote()
    with pytest.raises(AuthRequired):
        r._call(lambda: (_ for _ in ()).throw(PyiCloudAPIResponseException("gone", 421)))
    assert r.auth.expired == "HTTP 421"


def test_otros_errores_no_se_reintentan():
    r = _remote()
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("x")
    with pytest.raises(ValueError):
        r._call(bad)
    assert len(calls) == 1


class FakeResp:
    def __init__(self, data, code=200):
        self.data, self.status_code = data, code

    def iter_content(self, n):
        for i in range(0, len(self.data), 3):
            yield self.data[i:i + 3]

    def close(self):
        pass


def test_descarga_por_trozos_con_nombre_en_la_url(tmp_path):
    seen = []
    session = SimpleNamespace(get=lambda url, **kw: seen.append(url) or FakeResp(b"abcdefgh"))
    r = _remote(SimpleNamespace(session=session))
    dest = tmp_path / "2026/10/IMG 1.HEIC"
    n = r.download(RemoteFile("original", "IMG 1.HEIC", "https://x/${f}?o=1", 8, None), dest, 1_700_000_000)
    assert n == 8 and dest.read_bytes() == b"abcdefgh" and dest.stat().st_mtime == 1_700_000_000
    assert seen == ["https://x/${f}?o=1"]          # primero la URL tal cual (como pyicloud)


def test_descarga_si_apple_da_410_prueba_con_el_nombre(tmp_path):
    seen = []

    def get(url, **kw):
        seen.append(url)
        if "${f}" in url:
            raise PyiCloudAPIResponseException("gone", 410)
        return FakeResp(b"abcd")
    r = _remote(SimpleNamespace(session=SimpleNamespace(get=get)))
    n = r.download(RemoteFile("original", "a b.JPG", "https://x/${f}?o=1", 4, None), tmp_path / "f.JPG", 0)
    assert n == 4 and seen == ["https://x/${f}?o=1", "https://x/a%20b.JPG?o=1"]


def test_descarga_410_en_las_dos_variantes_falla_sin_reintentar(tmp_path):
    seen = []

    def get(url, **kw):
        seen.append(url)
        raise PyiCloudAPIResponseException("gone", 410)
    r = _remote(SimpleNamespace(session=SimpleNamespace(get=get)))
    with pytest.raises(PyiCloudAPIResponseException):
        r.download(RemoteFile("original", "a.JPG", "https://x/${f}", 4, None), tmp_path / "a.JPG", 0)
    assert len(seen) == 2


def test_descarga_incompleta_no_deja_nada(tmp_path):
    session = SimpleNamespace(get=lambda url, **kw: FakeResp(b"abc"))
    r = _remote(SimpleNamespace(session=session))
    dest = tmp_path / "f.HEIC"
    with pytest.raises(IOError):
        r.download(RemoteFile("original", "f.HEIC", "https://x", 8, None), dest, 0)
    assert list(tmp_path.iterdir()) == []


class FakeAlbum:
    page_size = 4

    def __init__(self, assets):
        self.assets = assets
        self.offsets = []

    def _get_photos_at(self, offset, direction, page_size):
        self.offsets.append(offset)
        return iter(self.assets[offset:offset + page_size * 2])


def _asset(i):
    res = SimpleNamespace(filename=f"IMG_{i}.HEIC", url="u", size=1, checksum="c")
    return SimpleNamespace(id=f"a{i}", resources={"original": res}, filename=f"IMG_{i}.HEIC",
                           asset_date=None, item_type="image", is_live_photo=False,
                           _asset_record={"fields": {}})


def test_listado_paginado_sin_duplicados():
    album = FakeAlbum([_asset(i) for i in range(19)])
    lib = SimpleNamespace(all=album)
    api = SimpleNamespace(photos=SimpleNamespace(libraries={"root": lib}))
    r = _remote(api)
    ids = [a.id for a in r.list_assets(keep={"a3"})]
    assert ids == [f"a{i}" for i in range(19)]
    assert list(r._handles) == ["a3"]           # solo se guarda el objeto de las que se pueden borrar
    assert album.offsets == [0, 8, 16, 19]      # mismo criterio de parada que pyicloud (página casi vacía)


def _fresh_lookup(r, url):
    def lookup(ids):
        fresh = describe_fake(url)
        r.hidden_assets = {}
        return {i: fresh for i in ids}
    r.lookup = lookup


def describe_fake(url):
    from services.photos_remote import RemoteAsset
    ra = RemoteAsset("a1", "IMG_1.HEIC", None, "image")
    ra.files["original"] = RemoteFile("original", "IMG_1.HEIC", url, 4, None)
    return ra


def test_url_caducada_se_pide_otra_vez_por_id(tmp_path):
    seen = []

    def get(url, **kw):
        seen.append(url)
        if "vieja" in url:
            raise PyiCloudAPIResponseException("gone", 410)
        return FakeResp(b"abcd")
    r = _remote(SimpleNamespace(session=SimpleNamespace(get=get)))
    _fresh_lookup(r, "https://nueva/${f}")
    rf = RemoteFile("original", "IMG_1.HEIC", "https://vieja/${f}", 4, None)
    assert r.download(rf, tmp_path / "IMG_1.HEIC", 0, asset_id="a1") == 4
    assert seen[-1] == "https://nueva/${f}" and len(seen) == 3


def test_url_vieja_se_relee_antes_de_bajar(tmp_path):
    seen = []
    session = SimpleNamespace(get=lambda url, **kw: seen.append(url) or FakeResp(b"abcd"))
    r = _remote(SimpleNamespace(session=session))
    _fresh_lookup(r, "https://nueva/${f}")
    rf = RemoteFile("original", "IMG_1.HEIC", "https://vieja/${f}", 4, None, listed_at=time.time() - 3600)
    r.download(rf, tmp_path / "IMG_1.HEIC", 0, asset_id="a1")
    assert seen == ["https://nueva/${f}"]          # ni siquiera se intenta la caducada


def test_renovar_urls_de_ocultas():
    from services.photos_remote import HIDDEN
    r = _remote()
    fresh = describe_fake("https://nueva/${f}")

    def lookup(ids):
        r.hidden_assets = {"h1": fresh}
        return {"h1": HIDDEN}
    r.lookup = lookup
    rf = RemoteFile("original", "IMG_1.HEIC", "https://vieja/${f}", 4, None)
    assert r.refresh_urls([("h1", rf)]) == 1 and rf.url == "https://nueva/${f}"


class AddedAlbum:
    """Índice ADDED como lo describe pyicloud: la posición n devuelve las más nuevas hasta n (con hasta
    2 páginas de resultados) y la ventana llega de la más vieja a la más nueva."""
    page_size = 4

    def __init__(self, assets):
        self.assets = assets          # de la más nueva a la más vieja

    def _get_photos_at(self, offset, direction, page_size):
        lo = max(0, offset + 1 - page_size * 2)
        return iter(list(reversed(self.assets[lo:offset + 1])))


def test_pagina_mas_reciente_primero_sin_repetir():
    assets = [_asset(i) for i in range(10)]           # a0 = la más nueva
    lib = SimpleNamespace(recently_added=lambda: AddedAlbum(assets))
    api = SimpleNamespace(photos=SimpleNamespace(libraries={"root": lib}))
    r = _remote(api)
    r.new_run()
    seen, pos = [], 0
    while True:
        page, advance, raw, size = r.page_newest(pos)
        if not raw:
            break
        seen += [a.id for a in page]
        pos += advance
        if raw < size // 2 or not advance:
            break
    assert seen == [f"a{i}" for i in range(10)]


def test_pagina_salta_ocultas_y_borradas():
    assets = [_asset(i) for i in range(3)]
    assets[1]._asset_record = {"fields": {"isHidden": {"value": 1}}}
    lib = SimpleNamespace(recently_added=lambda: AddedAlbum(assets))
    r = _remote(SimpleNamespace(photos=SimpleNamespace(libraries={"root": lib})))
    page, advance, raw, _ = r.page_newest(0)
    assert [a.id for a in page] == ["a0", "a2"] and raw == 3 and advance == 3   # la oculta también cuenta


class LoopAlbum:
    """Álbum cuyo índice no deja de devolver fotos (lo que se temía del paginado de pyicloud)."""
    page_size = 4
    name = "Bucle"

    def __len__(self):
        return 6

    def __init__(self):
        self.calls = 0

    def _get_photos_at(self, offset, direction, page_size):
        self.calls += 1
        return iter([_asset(i) for i in range(offset, offset + page_size)])


def test_album_se_para_al_tener_lo_que_dice_apple():
    album = LoopAlbum()
    got = _remote().album_assets(album)
    assert album.calls == 2 and len(got) == 8


class OverlapAlbum:
    """Álbum cuyas páginas se solapan (lo que pasaba con Oculto, Islandia…)."""
    page_size = 4
    name = "Solapes"

    def __init__(self, n):
        self.items = [_asset(i) for i in range(n)]

    def __len__(self):
        return len(self.items)

    def _get_photos_at(self, offset, direction, page_size):
        lo = max(0, offset - 2)                       # cada página repite 2 de la anterior
        return iter(self.items[lo:lo + page_size])


def test_album_con_paginas_solapadas_sale_completo():
    got = _remote().album_assets(OverlapAlbum(30))
    assert len({a.id for a in got}) == 30



def test_corte_de_red_envuelto_por_pyicloud_se_reintenta():
    """5/11: RemoteDisconnected llegó como PyiCloudAPIResponseException("Request failed to iCloud") y mató la vuelta."""
    import requests
    r = _remote()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            try:
                raise requests.exceptions.ConnectionError("Remote end closed connection without response")
            except requests.exceptions.ConnectionError as err:
                raise PyiCloudAPIResponseException("Request failed to iCloud") from err
        return "ok"
    assert r._call(flaky) == "ok" and len(calls) == 2


def test_descarga_a_goteo_se_corta_por_tiempo_y_no_deja_nada(tmp_path):
    session = SimpleNamespace(get=lambda url, **kw: FakeResp(b"abcdefgh"))
    r = ICloudPhotosRemote(FakeAuth(SimpleNamespace(session=session)), sleep=lambda s: None, max_file_s=-1)
    dest = tmp_path / "f.HEIC"
    from services.photos_remote import SlowDownload
    with pytest.raises(SlowDownload):
        r.download(RemoteFile("original", "f.HEIC", "https://x", 8, None), dest, 0)
    assert list(tmp_path.iterdir()) == []


def test_corte_de_red_se_reintenta_mas_veces_que_un_frenazo():
    import requests
    r = _remote()
    calls = []

    def down():
        calls.append(1)
        raise requests.exceptions.ConnectionError("sin red")
    with pytest.raises(requests.exceptions.ConnectionError):
        r._call(down)
    assert len(calls) == 6


def test_lista_de_cambios_con_un_registro_raro_se_lee_sin_validar():
    """6/11: un registro sin tipo ni creación (deleted=False) hacía fallar toda la página en pyicloud."""
    from pyicloud.common.cloudkit.client import CloudKitApiError

    class Lib:
        zone_id = {"zoneName": "PrimarySync", "ownerRecordName": "_o", "zoneType": "REGULAR_CUSTOM_ZONE"}
        _current_sync_token = "viejo"

        def iter_changes(self, since=None):
            raise CloudKitApiError("Changes response validation failed", payload={})
            yield

    pages = [
        {"zones": [{"syncToken": "t1", "moreComing": True, "records": [
            {"recordName": "A1", "recordType": "CPLAsset", "deleted": False},
            {"recordName": "PrimarySync/X35C", "deleted": False},                      # el raro
            {"recordName": "B2", "deleted": True},
            {"recordName": "R1", "recordType": "CPLContainerRelation", "deleted": False}]}]},
        {"zones": [{"syncToken": "t2", "moreComing": False, "records": [
            {"recordName": "A3", "recordType": "CPLAsset", "deleted": False}]}]},
    ]
    sent = []
    lib = Lib()
    # El cliente REAL de pyicloud (PhotosCloudKitClient envuelve a CloudKitContainerClient); solo la capa HTTP es falsa.
    # Una versión anterior de este test simulaba la estructura y no cazó que raw_post vive un nivel más abajo.
    from pyicloud.services.photos_cloudkit.client import PhotosCloudKitClient
    client = PhotosCloudKitClient(base_url="https://x", session=SimpleNamespace(), base_params={})
    client._client._http.post = lambda path, payload, **kw: sent.append(payload) or pages[len(sent) - 1]
    lib._client = client
    r = _remote(SimpleNamespace(photos=SimpleNamespace(libraries={"root": lib})))
    changed, deleted, token = r.changes("t0")
    assert changed == {"A1", "A3"} and deleted == {"B2"} and token == "t2" and r.albums_changed
    assert sent[1]["zones"][0]["syncToken"] == "t1"            # sigue la paginación
