# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""JPG de alta calidad al lado de cada HEIC de la carpeta sincronizada, para verlos en Windows.

Los HEIC de iCloud Drive son documentos, no la fototeca: por ahora se DUPLICAN (prueba antes de
plantear sustituirlos). El JPG se crea en el NAS y la sync normal lo sube a iCloud.

Calidad: JPEG 95 sin submuestreo de color (4:4:4), con el EXIF completo (fecha, GPS, cámara), XMP,
perfil de color (Display P3 en los iPhone) y la fecha de modificación del original. pillow-heif ya
aplica el giro al decodificar, así que el EXIF sale con Orientation=1 (si no, Windows lo giraría dos
veces).

Reglas: nunca se toca un HEIC; nunca se borra un JPG; si el usuario borra un JPG, no se vuelve a
crear; si cambia el HEIC se regenera su JPG salvo que el JPG lo haya editado el usuario; si ya hay un
.jpg con ese nombre que no es nuestro, no se toca.
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path

from services.entries import RunResult
from services.local_fs import TMP_PREFIX, in_scope, is_excluded, nfc
from services.progress import Progress

log = logging.getLogger(__name__)

HEIC_EXTS = (".heic", ".heif")


class ConversionDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS conv (src TEXT PRIMARY KEY, src_size INTEGER, src_mtime_ns INTEGER, "
                "jpg TEXT, jpg_size INTEGER, jpg_mtime_ns INTEGER)"
            )
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def all(self) -> dict[str, tuple]:
        with self._lock:
            return {r[0]: r[1:] for r in self._conn.execute("SELECT * FROM conv")}

    def put(self, src: str, src_size: int, src_mtime_ns: int, jpg: str, jpg_size: int, jpg_mtime_ns: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO conv VALUES (?,?,?,?,?,?)",
                               (src, src_size, src_mtime_ns, jpg, jpg_size, jpg_mtime_ns))

    def move(self, old_src: str, new_src: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE conv SET src = ? WHERE src = ?", (new_src, old_src))

    def forget(self, src: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM conv WHERE src = ?", (src,))


ALGO = "primary-unless-preview"   # versión del criterio: si cambia, se revisan los JPG ya hechos

PREVIEW_MIN_RATIO = 4        # la otra imagen debe tener al menos 4 veces los píxeles de la principal
ASPECT_TOLERANCE = 0.01      # y la misma proporción (±1 %)
SAME_PHOTO_MAX_DIFF = 12     # y ser la misma foto: diferencia media en miniatura gris 32x32 (0-255)


def _primary_frame(im) -> int:
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        if im.info.get("primary", i == 0):
            return i
    return 0


def _thumb(im):
    return im.convert("L").resize((32, 32))


def _pick_frame(im) -> tuple[int, str]:
    """Qué imagen del HEIC se convierte: SIEMPRE la principal, salvo que la principal sea solo una
    vista previa reducida de otra imagen del fichero. Eso exige a la vez: la otra tiene al menos
    PREVIEW_MIN_RATIO veces sus píxeles, la misma proporción y, comparadas en miniatura, son la
    misma foto. Una imagen distinta (ráfaga, montaje) nunca sustituye a la principal."""
    n = getattr(im, "n_frames", 1)
    p = _primary_frame(im)
    if n <= 1:
        return p, ""
    im.seek(p)
    pw, ph = im.size
    candidates = []
    for i in range(n):
        if i == p:
            continue
        im.seek(i)
        w, h = im.size
        if w * h >= PREVIEW_MIN_RATIO * pw * ph and abs(w / h - pw / ph) <= ASPECT_TOLERANCE * (pw / ph):
            candidates.append((w * h, i))
    if not candidates:
        return p, ""
    _, best = max(candidates)
    im.seek(p)
    small = _thumb(im)
    im.seek(best)
    big = _thumb(im)
    diff = sum(abs(a - b) for a, b in zip(small.tobytes(), big.tobytes())) / (32 * 32)
    if diff > SAME_PHOTO_MAX_DIFF:
        return p, f"otra imagen más grande pero distinta (diferencia {diff:.0f}); se usa la principal"
    return best, f"la principal ({pw}x{ph}) es una vista previa de la foto de {im.size[0]}x{im.size[1]}"


def describe(src: Path) -> str:
    """Estructura del HEIC para el log: imágenes, tamaños y cuál es la principal."""
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    parts = []
    with Image.open(src) as im:
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            parts.append(f"{im.size[0]}x{im.size[1]}{' (principal)' if im.info.get('primary', i == 0) else ''}")
    return f"{len(parts)} imagen(es): " + ", ".join(parts)


def chosen_size(src: Path) -> tuple[tuple[int, int], int, int]:
    """(tamaño de la imagen elegida, índice elegido, índice principal)."""
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(src) as im:
        p = _primary_frame(im)
        idx, _ = _pick_frame(im)
        im.seek(idx)
        return im.size, idx, p


def convert(src: Path, dst: Path, quality: int = 95) -> None:
    """HEIC → JPEG conservando metadatos. Escribe en un temporal y lo mueve al final."""
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    pillow_heif.options.DECODE_THREADS = 2   # que la conversión no se coma todo el NAS

    tmp = dst.with_name(f"{TMP_PREFIX}{dst.name}")
    try:
        with Image.open(src) as im:
            # Metadatos de la principal por si la imagen elegida no los trae
            p = _primary_frame(im)
            im.seek(p)
            p_exif, p_icc, p_xmp = im.getexif(), im.info.get("icc_profile"), im.info.get("xmp")
            best, why = _pick_frame(im)
            im.seek(best)
            exif = im.getexif() if len(im.getexif()) else p_exif
            exif[0x0112] = 1                      # el giro ya viene aplicado en los píxeles
            icc = im.info.get("icc_profile") or p_icc
            xmp = im.info.get("xmp") or p_xmp
            size = im.size
            if why:
                log.info("%s: %s", src.name, why)
            rgb = im.convert("RGB") if im.mode != "RGB" else im
            kwargs = {"quality": quality, "subsampling": 0, "optimize": True, "exif": exif.tobytes()}
            if icc:
                kwargs["icc_profile"] = icc
            if xmp:
                kwargs["xmp"] = xmp
            rgb.save(tmp, "JPEG", **kwargs)
        with Image.open(tmp) as check:
            if check.size != size:
                raise IOError(f"el JPG salió de {check.size} en vez de {size}")
        st = src.stat()
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


class HeicToJpg:
    def __init__(self, db: ConversionDB, root: Path, excludes: list[str], folders: list[str],
                 quality: int = 95, progress: Progress | None = None):
        self.db, self.root, self.excludes, self.folders = db, root, excludes, folders
        self.quality, self.progress = quality, progress

    def _scan(self) -> tuple[list[str], dict[str, set[str]]]:
        """HEIC (rutas relativas reales) y nombres de cada carpeta, para ver colisiones."""
        heics, names = [], {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames
                           if not is_excluded(nfc(os.path.join(rel_dir, d)), self.excludes)
                           and in_scope(nfc(os.path.join(rel_dir, d)), self.folders)]
            names[rel_dir] = set(filenames)
            for f in filenames:
                rel = os.path.join(rel_dir, f) if rel_dir else f
                if (f.lower().endswith(HEIC_EXTS) and not f.startswith(TMP_PREFIX)
                        and not is_excluded(nfc(rel), self.excludes) and in_scope(nfc(rel), self.folders)
                        and not os.path.islink(os.path.join(dirpath, f))):
                    heics.append(rel)
        return sorted(heics), names

    def _target(self, rel: str, names: dict[str, set[str]], claimed: set[str]) -> str | None:
        rel_dir, fname = os.path.split(rel)
        stem, ext = os.path.splitext(fname)
        taken = {n.casefold() for n in names.get(rel_dir, set())}
        plain = f"{stem}.jpg"
        if plain.casefold() not in taken and os.path.join(rel_dir, plain) not in claimed \
                and f"{stem}.jpeg".casefold() not in taken:
            return os.path.join(rel_dir, plain)
        # Pareja por mayúsculas (x.heic y x.HEIC): el JPG del otro ya es nuestro
        other = os.path.join(rel_dir, plain)
        if other in claimed:
            alt = f"{stem} ({ext.lstrip('.').upper()}).jpg"
            if alt.casefold() not in taken:
                return os.path.join(rel_dir, alt)
        return None   # ya hay un JPG con ese nombre que no es nuestro: no se toca

    def _recheck(self, rows: dict, todo: list, result: RunResult) -> None:
        """Una vez por cambio de ALGO: JPG hechos a partir de una imagen más pequeña que la mayor del
        HEIC (vista previa en vez de la foto). Solo cabeceras; no se tocan JPG editados por el usuario."""
        queued = {t[0] for t in todo}
        for src, (src_size, src_mtime, jpg_name, jpg_size, jpg_mtime) in rows.items():
            jpg = os.path.join(os.path.dirname(src), jpg_name)
            s_path, j_path = self.root / src, self.root / jpg
            if src in queued or not jpg_name or not s_path.exists() or not j_path.exists():
                continue
            if (j_path.stat().st_size, j_path.stat().st_mtime_ns) != (jpg_size, jpg_mtime):
                continue
            try:
                from PIL import Image
                (w, h), idx, primary = chosen_size(s_path)
                if idx == primary:
                    continue                      # la principal: el JPG que hay es el correcto
                with Image.open(j_path) as j:
                    jpx = j.size[0] * j.size[1]
                if jpx < w * h:
                    todo.append((src, jpg, f"se había convertido una vista previa; ahora {w}x{h}"))
            except Exception as exc:
                result.warnings.append(f"No se pudo revisar {jpg}: {exc}")
        self._recheck_done = True

    def _follow_moves(self, heics: list[str], rows: dict) -> dict:
        """Filas cuyo HEIC ya no está donde se apuntó pero aparece con el mismo nombre, tamaño y fecha
        exactos en otra ruta (carpeta renombrada o movida): se actualiza la ruta y se conserva el
        historial. Si no, un JPG borrado a propósito se volvería a crear tras un renombrado."""
        present = set(heics)
        lost = {src: r for src, r in rows.items() if src not in present}
        if not lost:
            return rows
        known = set(rows)
        by_key: dict[tuple, list[str]] = {}
        for h in heics:
            if h in known:
                continue
            st = (self.root / h).stat()
            by_key.setdefault((os.path.basename(h), st.st_size, st.st_mtime_ns), []).append(h)
        for src, r in lost.items():
            cands = by_key.get((os.path.basename(src), r[0], r[1]), [])
            if len(cands) == 1:
                new = cands[0]
                self.db.move(src, new)
                rows[new] = rows.pop(src)
                by_key[(os.path.basename(src), r[0], r[1])] = []
                log.info("HEIC movido/renombrado: %s → %s (se conserva su historial)", src, new)
        return rows

    def run(self, dry_run: bool = False) -> RunResult:
        result = RunResult(dry_run=dry_run)
        if not self.root.is_dir():
            return result
        heics, names = self._scan()
        rows = self._follow_moves(heics, self.db.all())
        claimed = {os.path.join(os.path.dirname(src), r[2]) for src, r in rows.items() if r[2]}

        todo: list[tuple[str, str, str]] = []   # (src, jpg, motivo)
        for src in heics:
            st = (self.root / src).stat()
            row = rows.get(src)
            if row is None:
                target = self._target(src, names, claimed)
                if target is None:
                    continue
                claimed.add(target)
                todo.append((src, target, "HEIC nuevo"))
                continue
            src_size, src_mtime, jpg_name, jpg_size, jpg_mtime = row
            jpg = os.path.join(os.path.dirname(src), jpg_name)
            if (st.st_size, st.st_mtime_ns) == (src_size, src_mtime):
                continue                                   # hecho (o JPG borrado a propósito)
            jpg_path = self.root / jpg
            if not jpg_path.exists():
                self.db.put(src, st.st_size, st.st_mtime_ns, jpg_name, 0, 0)   # borrado: se respeta
            elif (jpg_path.stat().st_size, jpg_path.stat().st_mtime_ns) != (jpg_size, jpg_mtime):
                result.warnings.append(f"{src} cambió pero {jpg} lo has editado tú: no se regenera")
            else:
                todo.append((src, jpg, "el HEIC cambió"))
        if self.db.get_meta("algo") != ALGO:
            self._recheck(rows, todo, result)
        existing = set(heics)
        for src in rows:
            if src not in existing:
                self.db.forget(src)                       # el HEIC ya no está; su JPG se queda

        result.counts = {"heic_jpg": len(todo)} if todo else {}
        if not todo:
            if getattr(self, "_recheck_done", False) and not dry_run:
                self.db.set_meta("algo", ALGO)
            return result
        if dry_run:
            for src, jpg, why in todo:
                log.info("[dry-run] heic_jpg %s → %s (%s)", src, jpg, why)
            return result

        if self.progress:
            self.progress.set_phase("heic")
            self.progress.set_plan(len(todo), 0)   # avanza por fotos: los bytes no aplican aquí
        log.info("Creando %d JPG a partir de HEIC", len(todo))
        done = 0
        for src, jpg, why in todo:
            if self.progress and self.progress.stop_requested:
                result.status = "stopped"
                break
            size = (self.root / src).stat().st_size
            if self.progress:
                self.progress.action_start("heic_jpg", jpg, size)
            try:
                convert(self.root / src, self.root / jpg, self.quality)
                s, j = (self.root / src).stat(), (self.root / jpg).stat()
                if j.st_size < 0.5 * s.st_size:
                    result.warnings.append(f"{jpg} ocupa {j.st_size / s.st_size:.0%} de su HEIC: "
                                           f"{describe(self.root / src)}")
                self.db.put(src, s.st_size, s.st_mtime_ns, os.path.basename(jpg), j.st_size, j.st_mtime_ns)
                done += 1
                log.info("heic_jpg %s (%s)", jpg, why)
                if self.progress:
                    self.progress.action_done("heic_jpg", jpg, size, True, reason=why)
            except Exception as exc:
                msg = f"heic_jpg {src}: {type(exc).__name__}: {exc}"
                log.warning(msg)
                result.errors.append(msg)
                if self.progress:
                    self.progress.action_done("heic_jpg", jpg, size, False, error=f"{type(exc).__name__}: {exc}")
        result.counts = {"heic_jpg": done} if done else {}
        if getattr(self, "_recheck_done", False) and result.status == "ok" and not result.errors:
            self.db.set_meta("algo", ALGO)
        if result.errors and result.status == "ok":
            result.status = "partial"
        return result
