# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Mantenimiento de usuarios desde la terminal del contenedor (terminal del contenedor):

    python -m core.webauth usuario-nuevo <nombre>
    python -m core.webauth contraseña <nombre>
    python -m core.webauth desactivar <nombre>   |  activar <nombre>
    python -m core.webauth reset-totp <nombre>   (tendrá que volver a darlo de alta al entrar)
    python -m core.webauth lista

Rutas: AUTH_USERS_FILE (por defecto /data/config/usuarios.json) y AUTH_SECRET_KEY (o su fichero).
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
    p1 = getpass.getpass("Contraseña nueva (mín. 12): ")
    if p1 != getpass.getpass("Repítela: "):
        sys.exit("No coinciden")
    return p1


def main(argv: list[str]) -> None:
    if len(argv) < 1:
        sys.exit(__doc__)
    cmd, name = argv[0], (argv[1] if len(argv) > 1 else "")
    store = UserStore(Path(os.getenv("AUTH_USERS_FILE", "/data/config/usuarios.json")), SecretBox(_key(), "totp"))
    if cmd == "lista":
        data = store._load()["usuarios"]
        for n, u in data.items():
            print(f"{n}: {'activo' if u.get('activo', True) else 'DESACTIVADO'}, "
                  f"TOTP {'sí' if u.get('totp') else 'no'}, recuperación {store.recovery_left(n)}")
        return
    if not name:
        sys.exit("Falta el nombre de usuario")
    if cmd == "usuario-nuevo":
        store.create(name, _ask_password())
    elif cmd in ("contraseña", "contrasena", "password"):
        store.set_password(name, _ask_password())
    elif cmd == "desactivar":
        store.set_active(name, False)
    elif cmd == "activar":
        store.set_active(name, True)
    elif cmd == "reset-totp":
        store.set_totp(name, None)
        print("Segundo factor borrado: al entrar tendrá que darlo de alta de nuevo.")
    else:
        sys.exit(__doc__)
    print("Hecho.")


if __name__ == "__main__":
    main(sys.argv[1:])
