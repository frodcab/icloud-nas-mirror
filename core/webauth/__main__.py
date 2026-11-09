# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""User maintenance from the container's terminal (the Spanish command names keep working):

    python -m core.webauth new-user <name>          (usuario-nuevo)
    python -m core.webauth password <name>          (contraseña)
    python -m core.webauth disable <name>           |  enable <name>      (desactivar | activar)
    python -m core.webauth reset-totp <name>        (the user has to set it up again at the next sign-in)
    python -m core.webauth list                     (lista)

Paths: AUTH_USERS_FILE (default /data/config/usuarios.json) and AUTH_SECRET_KEY (or its file).
"""

import getpass
import os
import sys
from pathlib import Path

from .secretbox import SecretBox
from .users import UserStore


def _key() -> str:
    key = os.getenv("AUTH_SECRET_KEY", "")
    key_file = Path(os.getenv("AUTH_SECRET_KEY_FILE", "/data/config/auth_secret.key"))
    if not key and key_file.exists():
        key = key_file.read_text().strip()
    return key or "sin-clave-solo-para-operaciones-sin-totp"


def _ask_password() -> str:
    p1 = getpass.getpass("New password (min. 12): ")
    if p1 != getpass.getpass("Repeat it: "):
        sys.exit("They do not match")
    return p1


def main(argv: list[str]) -> None:
    if len(argv) < 1:
        sys.exit(__doc__)
    cmd, name = argv[0], (argv[1] if len(argv) > 1 else "")
    store = UserStore(Path(os.getenv("AUTH_USERS_FILE", "/data/config/usuarios.json")), SecretBox(_key(), "totp"))
    if cmd in ("lista", "list"):
        data = store._load()["usuarios"]
        for n, u in data.items():
            print(f"{n}: {'active' if u.get('activo', True) else 'DISABLED'}, "
                  f"TOTP {'yes' if u.get('totp') else 'no'}, recovery codes {store.recovery_left(n)}")
        return
    if not name:
        sys.exit("Missing username")
    if cmd in ("usuario-nuevo", "new-user"):
        store.create(name, _ask_password())
    elif cmd in ("contraseña", "contrasena", "password"):
        store.set_password(name, _ask_password())
    elif cmd in ("desactivar", "disable"):
        store.set_active(name, False)
    elif cmd in ("activar", "enable"):
        store.set_active(name, True)
    elif cmd == "reset-totp":
        store.set_totp(name, None)
        print("Second factor deleted: it will have to be set up again at the next sign-in.")
    else:
        sys.exit(__doc__)
    print("Done.")


if __name__ == "__main__":
    main(sys.argv[1:])
