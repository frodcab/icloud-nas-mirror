# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""TOTP (RFC 6238, 30 s, 6 dígitos) con anti-reutilización y códigos de recuperación."""

import hashlib
import hmac
import secrets
import time

import pyotp
import segno

STEP = 30
WINDOW = 1          # se acepta el paso anterior y el siguiente (relojes algo desajustados)


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, user: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user, issuer_name=issuer)


def qr_svg(uri: str) -> str:
    # omitsize: sin width/height fijos y con viewBox, para que el CSS lo escale entero (con tamaño
    # fijo se recortaba). border=4 es la zona blanca que piden los lectores de QR.
    return segno.make(uri, error="m").svg_inline(border=4, omitsize=True, dark="#000", light="#fff")


def verify(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """Devuelve el paso usado si el código vale y es posterior a last_step (no se reutiliza)."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    t = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(t.at(step * STEP), code):
            return step
    return None


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"   # sin caracteres ambiguos
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(n)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
