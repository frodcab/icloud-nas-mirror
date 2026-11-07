# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Documentos-paquete de iWork (Pages, Numbers, Keynote).

iCloud entrega el paquete en un zip que lleva dentro la carpeta del documento ("X.pages/..."). El
formato de "archivo único" de iWork es ese mismo zip pero con el contenido en la raíz: quitando ese
nivel, "X.pages" se abre con doble clic como cualquier documento.
"""

import os
import unicodedata
import zipfile
from pathlib import Path

from services.local_fs import TMP_PREFIX


def validate_zip(path: Path) -> list[str]:
    if not zipfile.is_zipfile(path):
        raise IOError("el paquete descargado no es un zip válido")
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise IOError(f"el zip del paquete está dañado ({bad})")
        return z.namelist()


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def wrapped_folder(names: list[str], doc_name: str) -> str | None:
    """Si todo el zip cuelga de una carpeta con el nombre del documento, devuelve ese nombre.

    Se compara en NFC: los zips de Apple guardan los nombres en NFD ("ó" como "o" + tilde)."""
    target = _nfc(doc_name.rstrip("/"))
    if names and all("/" in n and _nfc(n.split("/", 1)[0]) == target for n in names):
        return target
    return None


def flatten_package_zip(path: Path, doc_name: str) -> bool:
    """Quita la carpeta envolvente "doc_name/" del zip. True si lo ha reescrito."""
    names = validate_zip(path)
    if wrapped_folder(names, doc_name) is None:
        return False
    tmp = path.with_name(f"{TMP_PREFIX}{path.name}")   # si se corta, el escaneo lo limpia
    try:
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w") as dst:
            for info in src.infolist():
                inner = info.filename.split("/", 1)[1]   # sin la carpeta envolvente
                if not inner:
                    continue
                data = src.read(info) if not info.is_dir() else b""
                new = zipfile.ZipInfo(inner, date_time=info.date_time)
                new.compress_type = info.compress_type
                new.external_attr = info.external_attr
                dst.writestr(new, data)
        validate_zip(tmp)
        st = path.stat()
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, path)
        return True
    finally:
        tmp.unlink(missing_ok=True)
