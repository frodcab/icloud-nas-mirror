# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""JPG al lado de cada HEIC: calidad, metadatos, orientación y reglas de no pisar nada."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("pillow_heif")
from PIL import Image, JpegImagePlugin  # noqa: E402
import pillow_heif  # noqa: E402

from services.heic_to_jpg import ConversionDB, HeicToJpg  # noqa: E402
from services.progress import Progress  # noqa: E402

pillow_heif.register_heif_opener()
MTIME = 1_751_800_000


def make_heic(path: Path, orientation: int = 6, color=(255, 0, 0)):
    """HEIC 400x200 con un bloque de color arriba-izquierda, EXIF con giro, fecha y GPS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (400, 200), "white")
    img.paste(color, (0, 0, 50, 50))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif.get_ifd(0x8769)[0x9003] = "2026:07:06 12:34:56"
    exif.get_ifd(0x8825).update({1: "N", 2: (40.0, 25.0, 0.0), 3: "W", 4: (3.0, 42.0, 0.0)})
    img.save(path, exif=exif.tobytes(), xmp=b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    os.utime(path, (MTIME, MTIME))


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    conv = HeicToJpg(ConversionDB(tmp_path / "heic.db"), root, [".DS_Store", "@eaDir"], [], 95, Progress())
    return root, conv


def test_jpg_keeps_metadata_orientation_and_date(env):
    root, conv = env
    make_heic(root / "Personal/Sesiones/IMG_1.heic")
    res = conv.run()
    assert res.counts == {"heic_jpg": 1} and res.status == "ok", res.errors
    jpg = root / "Personal/Sesiones/IMG_1.jpg"
    with Image.open(jpg) as j:
        e = j.getexif()
        assert j.size == (200, 400)                              # girado una sola vez
        assert j.convert("RGB").getpixel((190, 10))[0] > 240     # el bloque rojo arriba a la derecha
        assert e.get(0x0112) == 1
        assert e.get_ifd(0x8769).get(0x9003) == "2026:07:06 12:34:56"
        assert e.get_ifd(0x8825).get(2) == (40.0, 25.0, 0.0)
        assert j.info.get("xmp")
        assert JpegImagePlugin.get_sampling(j) == 0               # 4:4:4
    assert int(jpg.stat().st_mtime) == MTIME
    assert (root / "Personal/Sesiones/IMG_1.heic").exists()      # el original no se toca
    assert conv.run().counts == {}                               # segunda pasada: nada


def test_deleted_jpg_is_not_recreated(env):
    root, conv = env
    make_heic(root / "a.heic")
    conv.run()
    (root / "a.jpg").unlink()
    assert conv.run().counts == {} and not (root / "a.jpg").exists()


def test_changed_heic_regenerates_but_not_over_user_edits(env):
    root, conv = env
    make_heic(root / "a.heic")
    make_heic(root / "b.heic")
    conv.run()
    b_jpg = root / "b.jpg"
    b_jpg.write_bytes(b"editado por el usuario")
    for n in ("a", "b"):
        make_heic(root / f"{n}.heic", color=(0, 0, 255))
        os.utime(root / f"{n}.heic", (MTIME + 60, MTIME + 60))
    res = conv.run()
    assert res.counts == {"heic_jpg": 1}
    with Image.open(root / "a.jpg") as j:
        assert j.convert("RGB").getpixel((190, 10))[2] > 240       # regenerado (ahora azul)
    assert b_jpg.read_bytes() == b"editado por el usuario"
    assert any("b.jpg" in w for w in res.warnings)


def test_existing_jpg_with_same_name_is_left_alone(env):
    root, conv = env
    make_heic(root / "IMG_2.HEIC")
    (root / "IMG_2.JPG").write_bytes(b"jpg del iPhone")
    assert conv.run().counts == {}
    assert (root / "IMG_2.JPG").read_bytes() == b"jpg del iPhone"


def test_case_variant_pair_gets_two_jpgs(env):
    root, conv = env
    make_heic(root / "IMG_0206.HEIC")
    make_heic(root / "IMG_0206.heic", color=(0, 255, 0))
    res = conv.run()
    assert res.counts == {"heic_jpg": 2}
    assert {p.name for p in root.glob("*.jpg")} == {"IMG_0206.jpg", "IMG_0206 (HEIC).jpg"}


def test_removed_heic_keeps_its_jpg(env):
    root, conv = env
    make_heic(root / "a.heic")
    conv.run()
    (root / "a.heic").unlink()
    conv.run()
    assert (root / "a.jpg").exists()


def test_broken_heic_is_an_error_not_a_crash(env):
    root, conv = env
    (root / "roto.heic").write_bytes(b"no es un heic")
    make_heic(root / "bueno.heic")
    res = conv.run()
    assert res.status == "partial" and res.counts == {"heic_jpg": 1}
    assert not (root / "roto.jpg").exists()
    assert not list(root.glob(".icloud-sync-tmp-*"))


def test_dry_run_and_stop(env):
    root, conv = env
    for i in range(3):
        make_heic(root / f"f{i}.heic")
    assert conv.run(dry_run=True).counts == {"heic_jpg": 3} and not list(root.glob("*.jpg"))
    conv.progress.start(False)
    conv.progress.stop_requested = True
    assert conv.run().status == "stopped" and not list(root.glob("*.jpg"))


def make_multi_heic(path: Path, same_photo: bool = True):
    """HEIC con dos imágenes: la principal pequeña y otra grande en segundo lugar. Si same_photo, la
    principal es una vista previa reducida de la grande; si no, son fotos distintas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    big = Image.new("RGB", (2400, 1800), "white")
    big.paste((255, 0, 0), (0, 0, 1200, 900))
    big.paste((0, 0, 255), (1200, 900, 2400, 1800))
    if same_photo:
        small = big.resize((400, 300))
    else:
        small = Image.new("RGB", (400, 300), (0, 160, 0))
        small.paste((250, 250, 0), (200, 0, 400, 150))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2026:07:06 10:00:00"
    small.save(path, save_all=True, append_images=[big], primary_index=0, exif=exif.tobytes())
    os.utime(path, (MTIME, MTIME))


def test_uses_full_photo_when_primary_is_its_preview(env):
    root, conv = env
    make_multi_heic(root / "IMG_0206 2.heic")
    assert conv.run().counts == {"heic_jpg": 1}
    with Image.open(root / "IMG_0206 2.jpg") as j:
        assert j.size == (2400, 1800)
        assert j.getexif().get_ifd(0x8769).get(0x9003) == "2026:07:06 10:00:00"


def test_keeps_primary_when_other_image_is_a_different_photo(env):
    root, conv = env
    make_multi_heic(root / "rafaga.heic", same_photo=False)
    conv.run()
    with Image.open(root / "rafaga.jpg") as j:
        assert j.size == (400, 300)                              # la principal, aunque haya otra mayor


def test_single_image_heic_always_uses_primary(env):
    from services.heic_to_jpg import chosen_size
    root, conv = env
    make_heic(root / "normal.heic")
    size, idx, primary = chosen_size(root / "normal.heic")
    assert idx == primary == 0 and size == (200, 400)


def test_recheck_only_touches_preview_cases(env):
    from services import heic_to_jpg
    root, conv = env
    make_multi_heic(root / "a.heic")                  # vista previa: se regenera
    make_multi_heic(root / "b.heic")                  # vista previa, pero JPG editado: no
    make_multi_heic(root / "c.heic", same_photo=False)   # otra foto distinta: no
    make_heic(root / "d.heic")                        # una sola imagen: no
    orig = heic_to_jpg._pick_frame
    heic_to_jpg._pick_frame = lambda im: (heic_to_jpg._primary_frame(im), "")   # versión anterior
    try:
        conv.run()
    finally:
        heic_to_jpg._pick_frame = orig
    conv.db._conn.execute("DELETE FROM meta")
    before = {n: (root / f"{n}.jpg").read_bytes() for n in "cd"}
    (root / "b.jpg").write_bytes(b"editado")
    res = conv.run()
    assert res.counts == {"heic_jpg": 1}
    with Image.open(root / "a.jpg") as j:
        assert j.size == (2400, 1800)
    assert (root / "b.jpg").read_bytes() == b"editado"
    assert {n: (root / f"{n}.jpg").read_bytes() for n in "cd"} == before
    assert conv.db.get_meta("algo") == heic_to_jpg.ALGO
    assert conv.run().counts == {}


def test_renamed_folder_keeps_history_and_deleted_jpg_stays_deleted(env):
    root, conv = env
    make_heic(root / "Playa /IMG_1.heic")
    make_heic(root / "Playa /IMG_2.heic")
    conv.run()
    (root / "Playa /IMG_1.jpg").unlink()            # el usuario borra un JPG
    os.rename(root / "Playa ", root / "Playa")        # la carpeta se renombra (quitando el espacio)
    res = conv.run()
    assert res.counts == {}
    assert not (root / "Playa/IMG_1.jpg").exists()    # no se recrea
    assert (root / "Playa/IMG_2.jpg").exists()
    assert set(conv.db.all()) == {"Playa/IMG_1.heic", "Playa/IMG_2.heic"}
