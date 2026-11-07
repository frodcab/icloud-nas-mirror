# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Usuarios en un fichero autocontenido (JSON en la carpeta de datos de la app).

Es la única fuente de identidades: sin Keycloak ni ningún proveedor externo. Guarda solo hashes
(argon2id) y el secreto TOTP cifrado. Se relee cuando cambia (mtime), así que editarlo a mano (p.ej.
desactivar un usuario) vale sin reiniciar. Si está roto o trae una contraseña en claro, no entra
NADIE y el fichero no se sobrescribe.
"""

import json
import logging
import os
import threading
from pathlib import Path

from .passwords import hash_password, is_argon2_hash
from .secretbox import SecretBox
from .totp import hash_recovery, new_recovery_codes

HELP = (
    "Usuarios del panel. Solo hashes argon2id: NUNCA escribas una contraseña en claro. Para crear o "
    "cambiar usuarios usa, dentro del contenedor: python -m core.webauth usuario-nuevo <nombre> | "
    "contraseña <nombre> | desactivar <nombre> | activar <nombre> | reset-totp <nombre>. "
    "\"activo\": false deja al usuario fuera al momento (sin reiniciar). \"email\" (opcional): dónde "
    "enviar el código de ¿has olvidado la contraseña? (si falta, a la dirección de avisos de la app)."
)


class UsersFileError(Exception):
    """usuarios.json roto o inseguro: no se deja entrar a nadie."""


class UserStore:
    def __init__(self, path: Path, box: SecretBox):
        self.path = path
        self.box = box
        self._lock = threading.RLock()
        self._cache: tuple[float, dict] | None = None

    # --- lectura --------------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return {"usuarios": {}}
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsersFileError(f"{self.path} no es un JSON válido: {exc}") from exc
        users = data.get("usuarios")
        if not isinstance(users, dict):
            raise UsersFileError(f"{self.path}: falta el objeto \"usuarios\"")
        for name, u in users.items():
            if not isinstance(u, dict) or not is_argon2_hash(u.get("hash", "")):
                raise UsersFileError(f"{self.path}: el usuario '{name}' no tiene un hash argon2 válido "
                                     "(¿contraseña en claro?). Usa python -m core.webauth contraseña")
        self._cache = (mtime, data)
        return data

    def exists(self) -> bool:
        return self.path.exists()

    def get(self, name: str) -> dict | None:
        with self._lock:
            u = self._load()["usuarios"].get(name)
            return dict(u) if u else None

    def is_active(self, name: str) -> bool:
        try:
            u = self.get(name)
        except UsersFileError:
            return False
        return bool(u and u.get("activo", True))

    def totp_secret(self, name: str) -> str | None:
        u = self.get(name)
        return self.box.decrypt(u["totp"]) if u and u.get("totp") else None

    # --- escritura (atómica) --------------------------------------------------------------------

    def _save(self, data: dict) -> None:
        data["_ayuda"] = HELP
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._cache = None

    def _update(self, name: str, fn) -> None:
        with self._lock:
            data = self._load()
            if name not in data["usuarios"]:
                raise KeyError(name)
            fn(data["usuarios"][name])
            self._save(data)

    def create(self, name: str, password: str, enforce_min: bool = True) -> None:
        with self._lock:
            data = self._load() if self.exists() else {"usuarios": {}}
            if name in data["usuarios"]:
                raise ValueError(f"el usuario '{name}' ya existe")
            data["usuarios"][name] = {"hash": hash_password(password, enforce_min), "activo": True,
                                      "totp": None, "recuperacion": []}
            self._save(data)

    def bootstrap(self, name: str, password: str) -> bool:
        """Si no hay fichero, lo crea con este usuario (solo el hash). True si lo ha creado."""
        with self._lock:
            if self.exists() or not name or not password:
                return False
            # La contraseña que ya existía se acepta tal cual (solo se guarda su hash); si es corta,
            # se avisa para cambiarla desde la página Cuenta.
            self.create(name, password, enforce_min=False)
            if len(password) < 12:
                logging.getLogger(__name__).warning(
                    "La contraseña inicial de '%s' tiene menos de 12 caracteres: cámbiala en /cuenta", name)
            return True

    def set_password_hash(self, name: str, new_hash: str) -> None:
        self._update(name, lambda u: u.__setitem__("hash", new_hash))

    def set_password(self, name: str, password: str) -> None:
        self.set_password_hash(name, hash_password(password))

    def set_active(self, name: str, active: bool) -> None:
        self._update(name, lambda u: u.__setitem__("activo", active))

    def set_totp(self, name: str, secret: str | None) -> None:
        enc = self.box.encrypt(secret) if secret else None
        self._update(name, lambda u: u.__setitem__("totp", enc))

    def new_recovery_codes(self, name: str) -> list[str]:
        codes = new_recovery_codes()
        self._update(name, lambda u: u.__setitem__(
            "recuperacion", [{"hash": hash_recovery(c), "usado": False} for c in codes]))
        return codes

    def clear_recovery_codes(self, name: str) -> None:
        self._update(name, lambda u: u.__setitem__("recuperacion", []))

    def use_recovery_code(self, name: str, code: str) -> bool:
        target = hash_recovery(code)
        u = self.get(name) or {}
        if not any(not rc.get("usado") and rc.get("hash") == target for rc in u.get("recuperacion", [])):
            return False                      # no se reescribe el fichero por un código inválido
        used = {"ok": False}

        def mark(u):
            for rc in u.get("recuperacion", []):
                if not rc.get("usado") and rc.get("hash") == target:
                    rc["usado"] = True
                    used["ok"] = True
                    return

        self._update(name, mark)
        return used["ok"]

    def recovery_left(self, name: str) -> int:
        u = self.get(name) or {}
        return sum(1 for rc in u.get("recuperacion", []) if not rc.get("usado"))
