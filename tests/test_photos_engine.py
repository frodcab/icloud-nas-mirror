# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Motor de la sync de iCloud Fotos contra una fototeca falsa (sin Apple)."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.entries import SyncAborted
from services.photos_db import PhotosDB
from services.photos_engine import PhotosEngine
from services.photos_limits import PhotosLimits
from services.photos_remote import GONE, HIDDEN, RemoteAsset, RemoteFile, Throttled


def asset(aid, name, when, *, live=False, edited=None, content=b"x" * 10):
    ra = RemoteAsset(aid, name, when, "image")
    ra.files["original"] = RemoteFile("original", name, f"u/{aid}/o", len(content), f"fp-{aid}-o")
    base = os.path.splitext(name)[0]
    if live:
        ra.files["live"] = RemoteFile("live", base + ".MOV", f"u/{aid}/l", 4, f"fp-{aid}-l")
    if edited:
        ra.files["edited"] = RemoteFile("edited", f"{base} (editada).JPG", f"u/{aid}/e", 5, edited)
    return ra


class FakeRemote:
    def __init__(self, assets=()):
        self.assets = {a.id: a for a in assets}
        self.budget = None
        self.deleted, self.uploaded = [], []
        self.cursor_value = "c1"
        self.short_listing = False
        self.throttle_downloads = False
        self.fail_downloads = False
        self.download_calls = 0
        self.next_id = 100

    # --- lo que imita a CloudKit: token = foto del estado; cambios = diferencia con esa foto ---------
    PAGE = 4

    def _state(self):
        st = {aid: tuple(sorted((r, f.fingerprint, f.size) for r, f in a.files.items()))
              for aid, a in self.assets.items()}
        st.update({aid: ("oculta",) for aid in self.hidden})
        return st

    # álbumes: {("Viajes", "Italia"): [ids]}; ocultas: {id: RemoteAsset} (fuera de "Todas las fotos")
    @property
    def album_members(self):
        if not hasattr(self, "_albums"):
            self._albums = {}
        return self._albums

    @property
    def hidden(self):
        if not hasattr(self, "_hidden"):
            self._hidden = {}
        return self._hidden

    def albums(self):
        out = [("album", list(path), ("a", path)) for path in self.album_members]
        return out + [("hidden", ["Oculto"], ("h",))]

    def album_assets(self, key):
        if key == ("h",):
            return list(self.hidden.values())
        return [self.assets[i] for i in self.album_members[key[1]] if i in self.assets]

    def hide(self, aid):
        self.hidden[aid] = self.assets.pop(aid)

    def unhide(self, aid):
        self.assets[aid] = self.hidden.pop(aid)

    def cursor(self):
        if not hasattr(self, "_tokens"):
            self._tokens = {}
        token = f"t{len(self._tokens)}"
        self._tokens[token] = self._state()
        return token

    def changes(self, since):
        old, new = self._tokens[since], self._state()
        changed = {a for a, v in new.items() if old.get(a) != v}
        deleted = set(old) - set(new)
        self.changes_calls = getattr(self, "changes_calls", 0) + 1
        return changed, deleted, self.cursor()

    def lookup(self, ids):
        self.lookups = getattr(self, "lookups", []) + [sorted(ids)]
        self.hidden_assets = {i: self.hidden[i] for i in ids if i in self.hidden}
        return {i: HIDDEN if i in self.hidden else self.assets.get(i, GONE) for i in ids}

    def refresh_urls(self, items):
        self.refreshed = getattr(self, "refreshed", []) + [len({a for a, _ in items})]
        for _aid, rf in items:
            rf.listed_at = time.time()
        return len(items)

    def page_newest(self, from_top):
        ordered = sorted(self.assets.values(), key=lambda a: (a.date, a.id), reverse=True)
        self.pages_read = getattr(self, "pages_read", 0) + 1
        window = ordered[from_top:from_top + self.PAGE]
        if not hasattr(self, "hidden_in_added"):
            self.hidden_in_added = set()
        # En "Añadidas recientemente" las ocultas también ocupan posición, pero no se devuelven como visibles
        page = [a for a in window if a.id not in self.hidden_in_added]
        return page, len(window), len(window), self.PAGE

    def page_at(self, pos):
        ordered = sorted(self.assets.values(), key=lambda a: (a.date, a.id), reverse=True)
        self.random_reads = getattr(self, "random_reads", []) + [pos]
        window = ordered[pos:pos + self.PAGE]
        return window, len(window), self.PAGE, 0.1

    def count(self):
        return len(self.assets)

    def list_assets(self, keep=frozenset()):
        items = list(self.assets.values())
        if self.short_listing:
            items = items[: len(items) // 2 + 1]
        yield from items

    def download(self, rf, dest, mtime, asset_id=""):
        self.download_calls += 1
        if self.fail_downloads:
            raise IOError("HTTP 410")
        if self.throttle_downloads:
            raise Throttled("HTTP 503")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"d" * rf.size)
        os.utime(dest, (mtime, mtime))
        return rf.size

    def upload(self, path):
        self.next_id += 1
        aid = f"up{self.next_id}"
        self.uploaded.append(path.name)
        size = path.stat().st_size
        ra = RemoteAsset(aid, path.name, datetime(2026, 5, 3, tzinfo=timezone.utc), "image")
        ra.files["original"] = RemoteFile("original", path.name, "u", size, f"fp-{aid}")
        self.assets[aid] = ra
        return aid, False

    def delete(self, asset_id):
        self.deleted.append(asset_id)
        self.assets.pop(asset_id, None) or self.hidden.pop(asset_id, None)


D1 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
D2 = datetime(2025, 3, 2, 10, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "iCloud"
    root.mkdir()

    def make(assets, **kw):
        remote = FakeRemote(assets)
        limits = kw.pop("limits", PhotosLimits(pause_s=0, list_pause_s=0))
        engine = PhotosEngine(PhotosDB(tmp_path / "photos.db"), root, tmp_path / "trash", remote, limits,
                              [".DS_Store", "@eaDir"], kw.pop("confirm", 5), min_age_s=0, sleep=lambda s: None, **kw)
        return engine, remote
    return root, make


def files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_primera_descarga_por_fecha_con_live_y_edicion(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, live=True, edited="e1"), asset("a2", "IMG_2.HEIC", D2)])
    result, _ = engine.run()
    assert result.status == "ok" and result.mode == "backfill"
    assert files(root) == ["2025/03/IMG_2.HEIC", "2026/10/IMG_1 (editada).JPG", "2026/10/IMG_1.HEIC",
                           "2026/10/IMG_1.MOV"]
    assert (root / "2026/10/IMG_1.HEIC").stat().st_mtime == D1.timestamp()
    assert result.counts["descargas"] == 4
    # Segunda vuelta: carga inicial acabada → solo la lista de cambios, sin leer páginas
    pages = remote.pages_read
    result, _ = engine.run()
    assert result.mode == "changes" and result.counts == {} and remote.pages_read == pages


def test_nombres_repetidos_en_el_mismo_mes(env):
    root, make = env
    engine, _ = make([asset("a1", "IMG_1.HEIC", D1, live=True), asset("a2", "IMG_1.HEIC", D1, live=True)])
    engine.run()
    assert files(root) == ["2026/10/IMG_1 (2).HEIC", "2026/10/IMG_1 (2).MOV", "2026/10/IMG_1.HEIC", "2026/10/IMG_1.MOV"]


def test_limite_por_run_y_el_resto_en_el_siguiente(env):
    root, make = env
    engine, _ = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1, live=True) for i in range(5)],
                     limits=PhotosLimits(max_downloads=3, pause_s=0, list_pause_s=0))
    result, budget = engine.run()
    assert len(files(root)) == 3 and budget.deferred["descargas"] > 0
    assert "Límite de la vuelta" in result.message
    engine.run()
    engine.run()
    engine.run()
    # Sin "(2)": el original que bajó en otro run que su vídeo conserva el mismo nombre
    assert len(files(root)) == 10 and not any("(2)" in f for f in files(root))


def test_subir_coloca_la_foto_en_su_mes(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    engine.run()
    (root / "Subir").mkdir(exist_ok=True)
    (root / "Subir" / "playa.jpg").write_bytes(b"p" * 7)
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert remote.uploaded == ["playa.jpg"] and result.counts["subidas"] == 1
    assert "2026/05/playa.jpg" in files(root) and not (root / "Subir" / ".subidos" / "playa.jpg").exists()
    assert not any(f.startswith("Subir/") for f in files(root))
    result, _ = engine.run()
    assert "playa" not in str(remote.uploaded[1:])      # no se vuelve a subir


def test_borrado_en_nas_borra_en_icloud(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, live=True), asset("a2", "IMG_2.HEIC", D1)])
    engine.run()
    (root / "2026/10/IMG_1.HEIC").unlink()
    result, _ = engine.run()
    assert remote.deleted == ["a1"] and result.counts["borradas_en_icloud"] == 1
    assert files(root) == ["2026/10/IMG_2.HEIC"]         # el vídeo del Live Photo va a la papelera
    assert (engine.local.trash_dir).exists()


def test_borrar_solo_el_video_no_toca_icloud_ni_lo_vuelve_a_bajar(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, live=True)])
    engine.run()
    (root / "2026/10/IMG_1.MOV").unlink()
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert remote.deleted == [] and result.counts.get("descartadas") == 1
    remote.cursor_value = "c3"
    engine.run()
    assert files(root) == ["2026/10/IMG_1.HEIC"]


def test_muchos_borrados_esperan_confirmacion(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(8)], confirm=5)
    engine.run()
    for i in range(7):
        (root / f"2026/10/IMG_{i}.HEIC").unlink()
    result, _ = engine.run()
    assert result.status == "pending" and result.pending_deletes == 7 and remote.deleted == []
    batch = engine.db.pending_info()[1]
    # Se descarta: vuelven a bajarse de iCloud
    engine.db.decide_batch(batch, "rejected")
    result, _ = engine.run()
    assert remote.deleted == [] and len(files(root)) == 8
    # Ahora se borran de verdad y se confirman
    for i in range(7):
        (root / f"2026/10/IMG_{i}.HEIC").unlink()
    engine.run()
    engine.db.decide_batch(engine.db.pending_info()[1], "confirmed")
    engine.run()
    assert sorted(remote.deleted) == [f"a{i}" for i in range(7)]


def test_borrada_en_icloud_va_a_la_papelera(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1), asset("a2", "IMG_2.HEIC", D1)])
    engine.run()
    del remote.assets["a1"]
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert files(root) == ["2026/10/IMG_2.HEIC"] and result.counts["a_papelera"] == 1
    assert list((engine.local.trash_dir).rglob("IMG_1.HEIC"))


def test_listado_incompleto_no_borra_nada_en_el_nas(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(4)])
    engine.run()
    remote.short_listing = True
    engine.db.set("last_full", "0")                 # toca la vuelta completa semanal
    result, _ = engine.run()
    assert result.mode == "full"
    assert len(files(root)) == 4 and any("incompleto" in w for w in result.warnings)


def test_fototeca_casi_vacia_aborta(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(30)])
    engine.run()
    remote.assets = dict(list(remote.assets.items())[:5])
    # Con la lista de cambios: 25 borradas en iCloud → esperan confirmación, no se toca nada
    result, _ = engine.run()
    assert result.mode == "changes" and result.status == "pending" and len(files(root)) == 30
    # En la vuelta completa semanal: aborta
    engine.db.set("last_full", "0")
    with pytest.raises(SyncAborted):
        engine.run()
    assert len(files(root)) == 30


def test_edicion_nueva_en_el_iphone_se_vuelve_a_bajar(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, edited="v1")])
    engine.run()
    remote.assets["a1"].files["edited"].fingerprint = "v2"
    remote.assets["a1"].files["edited"].size = 9
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert result.counts["descargas_editadas"] == 1
    assert (root / "2026/10/IMG_1 (editada).JPG").stat().st_size == 9
    assert list(engine.local.trash_dir.rglob("IMG_1 (editada).JPG"))    # la anterior, en la papelera
    # Edición deshecha: la copia editada sobra
    del remote.assets["a1"].files["edited"]
    remote.cursor_value = "c3"
    engine.run()
    assert files(root) == ["2026/10/IMG_1.HEIC"]


def test_movida_en_el_nas_se_sigue_sin_borrar_en_icloud(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    engine.run()
    (root / "Favoritas").mkdir()
    os.replace(root / "2026/10/IMG_1.HEIC", root / "Favoritas/IMG_1.HEIC")
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert remote.deleted == [] and result.counts.get("movidas_en_nas") == 1
    assert "Favoritas/IMG_1.HEIC" in engine.db.files()


def test_simulacion_no_toca_nada(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    result, _ = engine.run(dry_run=True)
    assert files(root) == [] and engine.db.files() == {} and result.counts["descargas"] == 1
    assert not (root / "Subir").exists()


def test_frenazos_de_apple_cortan_el_run(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(10)])
    remote.throttle_downloads = True
    result, budget = engine.run()
    assert budget.exhausted and result.status == "partial" and "frenando" in result.message
    assert engine.db.get("backfill_done") is None and engine.db.get("backfill_k") in (None, "0")


def test_fichero_aun_copiandose_no_se_sube(env, tmp_path):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    engine.min_age_s = 3600
    (root / "Subir").mkdir()
    (root / "Subir" / "nueva.jpg").write_bytes(b"n")
    engine.run()
    assert remote.uploaded == []
    old = time.time() - 7200
    os.utime(root / "Subir" / "nueva.jpg", (old, old))
    engine.run()
    assert remote.uploaded == ["nueva.jpg"]


def test_sin_carpeta_aborta(tmp_path):
    engine = PhotosEngine(PhotosDB(tmp_path / "p.db"), tmp_path / "no", tmp_path / "t", FakeRemote(),
                          PhotosLimits(), [], 5)
    with pytest.raises(SyncAborted):
        engine.run()
    assert not Path(tmp_path / "no").exists()


def test_subir_un_duplicado_va_a_la_papelera(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    engine.run()
    remote.upload = lambda path: ("otro-id", True)      # Apple: "ya la tenía", con un id que no es el de la foto
    (root / "Subir").mkdir(exist_ok=True)
    (root / "Subir" / "IMG_1.HEIC").write_bytes(b"x" * 10)
    remote.cursor_value = "c2"
    result, _ = engine.run()
    assert result.counts["ya_estaban"] == 1 and engine.db.uploads() == {}
    assert not any(f.startswith("Subir/") for f in files(root))
    assert list(engine.local.trash_dir.rglob("IMG_1.HEIC"))


def test_si_todas_las_descargas_fallan_se_corta_la_vuelta(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(300)])
    remote.fail_downloads = True
    result, budget = engine.run()
    assert remote.download_calls == 10                 # no 300: se corta tras 10 fallos seguidos
    assert result.status == "partial" and "seguidas han fallado" in result.message
    assert budget.used["descargas"] == 10 and engine.db.get("backfill_done") is None


def test_los_fallos_gastan_cupo_de_la_vuelta(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(8)],
                          limits=PhotosLimits(max_downloads=5, pause_s=0, list_pause_s=0))
    remote.fail_downloads = True
    engine.run()
    assert remote.download_calls == 5



# --- carga inicial por bloques y lista de cambios -------------------------------------------------

def _many(n, start=datetime(2020, 1, 1, tzinfo=timezone.utc)):
    from datetime import timedelta
    return [asset(f"a{i:03d}", f"IMG_{i:03d}.HEIC", start + timedelta(days=i)) for i in range(n)]


def test_la_carga_sigue_donde_se_quedo_sin_releer_todo(env):
    root, make = env
    engine, remote = make(_many(40), limits=PhotosLimits(max_downloads=8, pause_s=0, list_pause_s=0))
    engine.backfill_overlap = 4                        # en producción 200; aquí la fototeca es de 40
    engine.run()
    assert len(files(root)) == 8
    newest = sorted(files(root))[-1]
    assert newest.endswith("IMG_039.HEIC")             # lo más reciente primero
    for _ in range(3):
        engine.run()
    pages_before = remote.pages_read
    engine.run()                                       # 5.ª vuelta: va por la posición 32
    # Mira arriba (1 página ya completa) y salta cerca de donde iba: no relee las 8 páginas
    assert remote.pages_read - pages_before <= 6
    assert len(files(root)) == 40
    engine.run()
    assert engine.db.get("backfill_done") == "1"


def test_foto_nueva_durante_la_carga_se_baja_la_primera(env):
    root, make = env
    engine, remote = make(_many(20), limits=PhotosLimits(max_downloads=4, pause_s=0, list_pause_s=0))
    engine.run()
    engine.run()
    nueva = asset("zz", "NUEVA.HEIC", datetime(2026, 10, 25, tzinfo=timezone.utc))
    remote.assets["zz"] = nueva
    engine.run()
    assert "2026/10/NUEVA.HEIC" in files(root)


def test_lo_borrado_en_icloud_durante_la_carga_llega_por_la_lista_de_cambios(env):
    root, make = env
    engine, remote = make(_many(12), limits=PhotosLimits(max_downloads=6, pause_s=0, list_pause_s=0))
    engine.run()                                       # baja 6 (las más recientes)
    del remote.assets["a011"]                          # borrada en iCloud a mitad de carga (ya bajada)
    while engine.db.get("backfill_done") != "1":
        engine.run()
    assert "2020/01/IMG_011.HEIC" in files(root)       # durante la carga no se sabía
    result, _ = engine.run()                           # primera vuelta de cambios
    assert result.mode == "changes" and "2020/01/IMG_011.HEIC" not in files(root)
    assert result.counts.get("a_papelera") == 1


def test_una_foto_que_falla_siempre_no_bloquea_la_carga(env):
    root, make = env
    engine, remote = make(_many(12))
    real = remote.download

    def flaky(rf, dest, mtime, asset_id=""):
        if asset_id == "a010":
            raise IOError("HTTP 500 siempre")
        return real(rf, dest, mtime, asset_id)
    remote.download = flaky
    result, _ = engine.run()
    assert len(files(root)) == 11 and engine.db.get("backfill_done") == "1"
    assert any("IMG_010" in e for e in result.errors)


def test_recuperada_de_eliminados_no_se_toca(env):
    root, make = env
    engine, remote = make(_many(3))
    engine.run()
    remote.lookup = lambda ids: {i: GONE if i == "a001" else remote.assets.get(i, GONE) for i in ids}
    token_state = dict(remote.assets)
    del remote.assets["a001"]                          # Apple: en Eliminados recientemente
    remote.assets["a001"] = token_state["a001"]
    remote.assets["a001"].files["original"].fingerprint = "otra"   # para que salga en los cambios
    result, _ = engine.run()
    assert result.counts.get("a_papelera") == 1        # ≤ 5: se aplica (va a la papelera del NAS)


# --- álbumes y Oculto -----------------------------------------------------------------------------

def _force_albums(engine):
    engine.db.set("albums_dirty", "1")


def test_album_como_carpeta_de_enlaces_duros(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, live=True), asset("a2", "IMG_2.HEIC", D1)])
    remote.album_members[("Viajes", "Italia")] = ["a1"]
    engine.run()
    link, src = root / "Álbumes/Viajes/Italia/IMG_1.HEIC", root / "2026/10/IMG_1.HEIC"
    assert link.exists() and os.path.samefile(link, src)            # misma foto, sin ocupar más
    assert (root / "Álbumes/Viajes/Italia/IMG_1.MOV").exists()       # con su vídeo
    assert not (root / "Álbumes/Viajes/Italia/IMG_2.HEIC").exists()
    # Las carpetas de álbum no cuentan como fotos del NAS
    assert not any(p.startswith("Álbumes/") for p in engine._scan())
    # Sale del álbum en iCloud: se quita el enlace, la foto sigue en su mes
    remote.album_members[("Viajes", "Italia")] = ["a2"]
    _force_albums(engine)
    engine.run()
    assert not link.exists() and src.exists()
    assert os.path.samefile(root / "Álbumes/Viajes/Italia/IMG_2.HEIC", root / "2026/10/IMG_2.HEIC")
    # Álbum borrado en iCloud: carpeta fuera
    del remote.album_members[("Viajes", "Italia")]
    _force_albums(engine)
    engine.run()
    assert not (root / "Álbumes/Viajes").exists() and src.exists()


def test_borrar_un_enlace_del_album_no_toca_icloud(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    remote.album_members[("Fav",)] = ["a1"]
    engine.run()
    (root / "Álbumes/Fav/IMG_1.HEIC").unlink()
    engine.run()
    assert remote.deleted == [] and (root / "Álbumes/Fav/IMG_1.HEIC").exists()   # se vuelve a enlazar


def test_oculta_se_baja_directamente_a_oculto(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    remote.hidden["h1"] = asset("h1", "SECRETO.HEIC", D1)
    remote.album_members[("Fav",)] = ["h1", "a1"]
    engine.run()
    assert "Álbumes/Oculto/SECRETO.HEIC" in files(root)
    assert not any("SECRETO" in f for f in files(root) if not f.startswith("Álbumes/Oculto/"))  # ni en su mes ni en otros álbumes
    engine.db.set("last_full", "0")                   # la vuelta completa no la ve en "Todas las fotos"
    result, _ = engine.run()
    assert result.mode == "full" and "Álbumes/Oculto/SECRETO.HEIC" in files(root)


def test_ocultar_y_mostrar_en_el_iphone(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1, live=True), asset("a2", "IMG_2.HEIC", D1)])
    engine.run()
    remote.hide("a1")
    result, _ = engine.run()                          # la lista de cambios la trae: se mueve ya
    assert {"Álbumes/Oculto/IMG_1.HEIC", "Álbumes/Oculto/IMG_1.MOV"} <= set(files(root))
    assert "2026/10/IMG_1.HEIC" not in files(root) and result.counts.get("a_papelera") is None
    remote.unhide("a1")
    engine.run()
    assert "2026/10/IMG_1.HEIC" in files(root) and not (root / "Álbumes/Oculto/IMG_1.HEIC").exists()


def test_borrar_una_oculta_en_el_nas_la_borra_en_icloud(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    remote.hidden["h1"] = asset("h1", "SECRETO.HEIC", D1)
    engine.run()
    (root / "Álbumes/Oculto/SECRETO.HEIC").unlink()
    engine.run()
    assert remote.deleted == ["h1"]


# --- PHOTOS_HIDDEN / PHOTOS_HIDDEN_DIR -------------------------------------------------------------

def test_ocultas_en_otra_carpeta_del_home(env, tmp_path):
    root, make = env
    oculto = tmp_path / "dd_icloud_oculto"
    oculto.mkdir()
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1), asset("a2", "IMG_2.HEIC", D1)], hidden_dir=oculto)
    remote.hidden["h1"] = asset("h1", "SECRETO.HEIC", D1)
    engine.run()
    assert (oculto / "SECRETO.HEIC").exists() and not any("SECRETO" in f for f in files(root))
    assert "@oculto/SECRETO.HEIC" in engine.db.files()
    remote.hide("a1")                                  # ocultada en el iPhone → a la otra carpeta
    engine.run()
    assert (oculto / "IMG_1.HEIC").exists() and "2026/10/IMG_1.HEIC" not in files(root)
    remote.unhide("a1")                                # mostrada → vuelve a su mes
    engine.run()
    assert "2026/10/IMG_1.HEIC" in files(root) and not (oculto / "IMG_1.HEIC").exists()
    (oculto / "SECRETO.HEIC").unlink()                 # borrada en el NAS → borrada en iCloud
    engine.run()
    assert remote.deleted == ["h1"]
    engine.db.set("last_full", "0")                    # la vuelta completa no las toma por borradas
    remote.hidden["h2"] = asset("h2", "OTRA.HEIC", D1)
    engine.db.set("albums_dirty", "1")
    engine.run()
    assert (oculto / "OTRA.HEIC").exists()


def test_ocultas_desactivadas_ni_se_bajan_ni_se_tocan(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1), asset("a2", "IMG_2.HEIC", D1)], hidden_enabled=False)
    remote.hidden["h1"] = asset("h1", "SECRETO.HEIC", D1)
    engine.run()
    assert not any("SECRETO" in f for f in files(root))
    remote.hide("a1")                                  # ocultada después de bajarla: se queda donde está
    result, _ = engine.run()
    engine.db.set("last_full", "0")
    result, _ = engine.run()
    assert "2026/10/IMG_1.HEIC" in files(root) and result.counts.get("a_papelera") is None


def test_cambiar_la_carpeta_de_ocultas_las_lleva_sin_borrar_nada(env, tmp_path):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    remote.hidden["h1"] = asset("h1", "SECRETO.HEIC", D1)
    engine.run()
    assert "Álbumes/Oculto/SECRETO.HEIC" in files(root)
    oculto = tmp_path / "dd_icloud_oculto"
    oculto.mkdir()
    engine2 = PhotosEngine(engine.db, root, tmp_path / "trash", remote, engine.limits, [], 5, min_age_s=0,
                           sleep=lambda s: None, hidden_dir=oculto)
    engine2.run()
    assert (oculto / "SECRETO.HEIC").exists() and remote.deleted == []
    assert not (root / "Álbumes/Oculto/SECRETO.HEIC").exists()


# --- arreglos del 2026-10-26 ------------------------------------------------------------------------

def test_la_carga_avanza_aunque_la_pagina_sea_de_ocultas(env):
    root, make = env
    fotos = _many(20)
    engine, remote = make(fotos, limits=PhotosLimits(max_downloads=6, pause_s=0, list_pause_s=0))
    remote.hidden_in_added = {a.id for a in fotos[12:16]}     # una página entera de ocultas
    for _ in range(6):
        engine.run()
    assert int(engine.db.get("backfill_k")) >= 16 or engine.db.get("backfill_done") == "1"


def test_ocultas_por_bajar_cada_vuelta_con_la_mitad_del_cupo(env, tmp_path):
    root, make = env
    oculto = tmp_path / "dd_icloud_oculto"
    oculto.mkdir()
    engine, remote = make(_many(10), hidden_dir=oculto,
                          limits=PhotosLimits(max_downloads=6, pause_s=0, list_pause_s=0))
    for i in range(8):
        remote.hidden[f"h{i}"] = asset(f"h{i}", f"OCULTA_{i}.HEIC", D1)
    engine.run()                                       # revisa álbumes (apunta las 8) y baja hasta 3
    assert len(list(oculto.iterdir())) == 3 and len(files(root)) == 3   # la otra mitad, visibles
    engine.run()
    engine.run()
    assert len(list(oculto.iterdir())) == 8            # sin volver a leer el álbum Oculto
    assert engine.db.get("albums_at") is not None


def test_urls_caducadas_se_renuevan_en_bloque(env):
    root, make = env
    fotos = _many(5)
    engine, remote = make(fotos)
    for a in fotos:
        for rf in a.files.values():
            rf.listed_at = time.time() - 3600         # leídas hace una hora
    engine.albums.refresh = lambda *a, **k: None
    real = remote.page_newest
    remote.page_newest = lambda pos: (lambda r: (r[0], r[1], r[2], r[3]))(real(pos))
    engine._run_downloads([(a, a.files["original"], f"2026/10/{a.filename}", None) for a in fotos], engine_budget(engine))
    assert remote.refreshed == [5]                     # una sola renovación para las 5


def engine_budget(engine):
    from services.photos_limits import RunBudget
    engine._result, engine._dry, engine._unfinished, engine._streak = __import__("services.entries", fromlist=["RunResult"]).RunResult(), False, 0, 0
    return RunBudget(PhotosLimits(pause_s=0), sleep=lambda s: None)


def test_album_sin_nombre_va_a_su_carpeta(env):
    root, make = env
    engine, remote = make([asset("a1", "IMG_1.HEIC", D1)])
    engine.db.set("albums_json", __import__("json").dumps({"Álbumes/": ["a1"]}))   # como lo guardó la versión anterior
    engine.db.set("albums_at", str(time.time()))
    engine.run()
    assert (root / "Álbumes/Sin nombre/IMG_1.HEIC").exists()
    assert not [p for p in (root / "Álbumes").iterdir() if p.is_file()]



# --- ficheros que fallan: no se comen el cupo y se reintentan en otro orden -------------------------

def test_fichero_que_falla_espera_y_no_bloquea_a_los_demas(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(3)])
    remote.fail_downloads = True
    engine.run()
    assert len(engine.db.download_fails()) == 3 and not engine.db.files()
    remote.fail_downloads = False
    remote.assets["n"] = asset("n", "NEW.HEIC", D1)          # una foto nueva, sin fallos previos
    calls_before = remote.download_calls
    engine.run()
    # Las 3 que fallaron hace un momento esperan; la nueva sí se baja
    assert remote.download_calls == calls_before + 1
    assert [f.asset_id for f in engine.db.files().values()] == ["n"]


def test_tras_la_espera_se_reintentan_y_al_bajar_se_borra_el_fallo(env):
    root, make = env
    engine, remote = make([asset(f"a{i}", f"IMG_{i}.HEIC", D1) for i in range(3)])
    remote.fail_downloads = True
    engine.run()
    remote.fail_downloads = False
    with engine.db._lock:
        engine.db._conn.execute("UPDATE download_fails SET last_try = last_try - 86400")
    engine.db.set("last_full", str(time.time() - 2 * 86400))     # hace más de un día de la última completa
    assert engine.mode() == "full"
    engine.run()
    assert len(engine.db.files()) == 3 and engine.db.download_fails() == {}


def test_los_reintentos_van_despues_de_los_nuevos_y_en_orden_aleatorio(env):
    root, make = env
    engine, remote = make([])
    mk = lambda i, d: (asset(f"a{i}", f"I{i}.HEIC", d), None, f"p{i}", None)
    old = [mk(i, D1) for i in range(20)]
    new = [mk(100, D1), mk(101, D1)]
    for ra, *_ in old:
        engine.db.download_failed(ra.id, "original", "x", 0.0)           # fallo hace mucho: ya toca
    for d in old:
        d[0].files["original"] = RemoteFile("original", "x.HEIC", "u", 4, None)
    for d in new:
        d[0].files["original"] = RemoteFile("original", "x.HEIC", "u", 4, None)
    ordered = engine._order_downloads([(ra, ra.files["original"], p, o) for ra, _, p, o in old + new])
    ids = [d[0].id for d in ordered]
    assert ids[:2] == ["a100", "a101"] and sorted(ids[2:]) == sorted(f"a{i}" for i in range(20))
    assert ids[2:] != [f"a{i}" for i in range(20)]                       # barajadas (1 entre 20! de no estarlo)


def test_temporales_huerfanos_se_limpian_al_arrancar(env):
    root, make = env
    engine, remote = make([])
    (root / "2026/10").mkdir(parents=True)
    old, fresh = root / "2026/10/.a.HEIC.part", root / "2026/10/.b.HEIC.part"
    old.write_bytes(b"x"); fresh.write_bytes(b"y")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert engine.clean_orphan_parts() == 1 and not old.exists() and fresh.exists()


# --- sondas en páginas al azar durante la carga inicial -------------------------------------------

def test_paginas_al_azar_bajan_fotos_y_dejan_sonda(env):
    root, make = env
    engine, remote = make(_many(30), random_pages=3,
                          limits=PhotosLimits(max_downloads=100, pause_s=0, list_pause_s=0))
    result, _ = engine.run()
    assert len(remote.random_reads) >= 3                       # las sondas se hicieron
    probes = json.loads(engine.db.get("random_probe"))
    assert probes and {"pos", "items", "visible", "s"} <= set(probes[0])
    # el recorrido en orden no se rompe: tras varias vueltas está todo, sin duplicados ni borrados
    for _ in range(8):
        engine.run()
    assert len(files(root)) == 30 and engine.db.get("backfill_done") == "1"
    assert not result.pending_deletes


def test_pagina_al_azar_mas_alla_del_final_acorta_el_rango(env):
    root, make = env
    engine, remote = make(_many(8), random_pages=2)
    engine.db.set("backfill_k", "500")                          # el recorrido cree que hay 500; Apple solo tiene 8
    engine.run()
    assert engine.db.get("random_end")                          # se aprendió dónde acaba el índice
    assert int(engine.db.get("random_end")) < 500


def test_sin_paginas_al_azar_no_se_leen(env):
    root, make = env
    engine, remote = make(_many(8), random_pages=0)
    engine.run()
    assert not getattr(remote, "random_reads", [])


def test_pasado_el_final_del_indice_apple_repite_y_la_carga_termina(env):
    """5/11: Apple repetía la última ventana más allá del final y la carga recorría posiciones sin parar."""
    root, make = env
    engine, remote = make(_many(8))
    real, calls = remote.page_newest, []

    def beyond_end(from_top):
        calls.append(from_top)
        assert len(calls) < 200, "la carga no termina"
        if from_top >= 8:
            return [], 0, remote.PAGE, remote.PAGE         # ventana ya vista: 0 nuevas, pero Apple da elementos
        return real(from_top)
    remote.page_newest = beyond_end
    engine.run()
    assert engine.db.get("backfill_done") == "1" and len(files(root)) == 8
    assert len(calls) < 20
