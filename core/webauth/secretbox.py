# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Cifrado autenticado (AES-GCM) de secretos guardados en ficheros (p.ej. el secreto TOTP)."""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"


class SecretBox:
    def __init__(self, master_key: str | bytes, purpose: str):
        if isinstance(master_key, str):
            master_key = master_key.encode()
        if len(master_key) < 16:
            raise ValueError("AUTH_SECRET_KEY demasiado corta")
        # Una clave distinta por uso, derivada de la maestra
        self._aes = AESGCM(hashlib.sha256(master_key + b"|" + purpose.encode()).digest())

    def encrypt(self, plain: str) -> str:
        nonce = os.urandom(12)
        return PREFIX + base64.urlsafe_b64encode(nonce + self._aes.encrypt(nonce, plain.encode(), None)).decode()

    def decrypt(self, token: str) -> str:
        if not token.startswith(PREFIX):
            raise ValueError("formato de secreto desconocido")
        raw = base64.urlsafe_b64decode(token[len(PREFIX):])
        return self._aes.decrypt(raw[:12], raw[12:], None).decode()
