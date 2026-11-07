# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Login autocontenido para apps FastAPI: usuarios en un JSON propio (argon2id), segundo factor
TOTP, sesiones por cookie, bloqueo de IP progresivo y anti-CSRF. Sin proveedores externos.

Ver router.WebAuth para integrarlo en una app.
"""

from .router import WebAuth
from .users import UsersFileError, UserStore

__all__ = ["WebAuth", "UserStore", "UsersFileError"]
