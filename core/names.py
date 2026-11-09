# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Names of the folders and suffixes the app CREATES on disk, in the language chosen with NAMES_LANG (es | en).

They are separate from the interface language on purpose: these names are stored in the state databases (photos.db, state.db), so changing
them on an installation that already synced would make the app lose track of its files. Choose it BEFORE the first sync. The default is
`es` (what every existing installation uses); the public docker-compose template sets `en` for new installs.

Modules read these attributes at run time (`names.ALBUMS_DIR`), so `load()` can switch language in tests.
"""

import os

SETS = {
    "es": {"ALBUMS_DIR": "Álbumes", "HIDDEN_NAME": "Oculto", "UNNAMED": "Sin nombre", "EDITED": "(editada)", "CONFLICT": "conflicto NAS",
           "UPLOAD_DIR": "Subir", "UPLOADED": ".subidos", "REJECTED": "_no_admitidas", "PHOTOS_TRASH": "Fotos"},
    "en": {"ALBUMS_DIR": "Albums", "HIDDEN_NAME": "Hidden", "UNNAMED": "Unnamed", "EDITED": "(edited)", "CONFLICT": "NAS conflict",
           "UPLOAD_DIR": "Upload", "UPLOADED": ".uploaded", "REJECTED": "_rejected", "PHOTOS_TRASH": "Photos"},
}
DEFAULT = "es"
EXTERNAL_HIDDEN = "@oculto"        # prefix used inside photos.db for a hidden folder outside Photos/: internal, never shown on disk

LANG = DEFAULT
ALBUMS_DIR = HIDDEN_NAME = HIDDEN_DIR = UNNAMED = EDITED_SUFFIX = CONFLICT = UPLOAD_DIR = UPLOADED = REJECTED = PHOTOS_TRASH = ""
SUFFIX: dict[str, str] = {}


def load(lang: str | None = None) -> str:
    """Select the language of the on-disk names; unknown values fall back to the default. Returns the language in use."""
    global LANG, ALBUMS_DIR, HIDDEN_NAME, HIDDEN_DIR, UNNAMED, EDITED_SUFFIX, CONFLICT, UPLOAD_DIR, UPLOADED, REJECTED, PHOTOS_TRASH, SUFFIX
    lang = (lang or os.getenv("NAMES_LANG") or DEFAULT).strip().lower()
    s = SETS.get(lang) or SETS[DEFAULT]
    LANG = lang if lang in SETS else DEFAULT
    ALBUMS_DIR, HIDDEN_NAME, UNNAMED, CONFLICT = s["ALBUMS_DIR"], s["HIDDEN_NAME"], s["UNNAMED"], s["CONFLICT"]
    HIDDEN_DIR = f"{ALBUMS_DIR}/{HIDDEN_NAME}"
    EDITED_SUFFIX = f" {s['EDITED']}"
    UPLOAD_DIR, UPLOADED, REJECTED, PHOTOS_TRASH = s["UPLOAD_DIR"], s["UPLOADED"], s["REJECTED"], s["PHOTOS_TRASH"]
    SUFFIX = {"original": "", "live": "", "edited": EDITED_SUFFIX, "edited_live": EDITED_SUFFIX}
    return LANG


load()
