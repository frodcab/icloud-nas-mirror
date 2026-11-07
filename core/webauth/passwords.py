# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Contraseñas con argon2id (parámetros por defecto de argon2-cffi, con rehash si cambian)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
_ph = PasswordHasher()
# Hash de relleno: verificar contra él cuando el usuario no existe cuesta lo mismo que uno real,
# así el tiempo de respuesta no revela qué usuarios existen.
_DUMMY = _ph.hash("contraseña de relleno que nunca coincide")


def hash_password(password: str, enforce_min: bool = True) -> str:
    if enforce_min and len(password) < MIN_LENGTH:
        raise ValueError(f"La contraseña debe tener al menos {MIN_LENGTH} caracteres")
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(stored_hash or _DUMMY, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False


def is_argon2_hash(value: str) -> bool:
    return isinstance(value, str) and value.startswith("$argon2")
