# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Avisos por email (SMTP + STARTTLS). Sin SMTP_USERNAME/SMTP_PASSWORD quedan desactivados."""

import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, host: str, port: int, username: str, password: str, to: str):
        self.host, self.port = host, port
        self.username, self.password, self.to = username, password, to
        self.enabled = bool(username and password and to)
        if not self.enabled:
            log.info("Avisos por email desactivados (faltan SMTP_USERNAME / SMTP_PASSWORD)")

    def send(self, subject: str, body: str, to: str | None = None) -> bool:
        """Nunca lanza: un fallo del correo no puede tumbar la sincronización. True si se ha enviado."""
        if not self.enabled:
            log.info("[aviso no enviado] %s", subject)
            return False
        msg = EmailMessage()
        msg["Subject"] = f"[iCloud sync] {subject}"
        msg["From"] = self.username
        msg["To"] = to or self.to
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
            log.info("Aviso enviado: %s", subject)
            return True
        except Exception as exc:
            log.error("No se pudo enviar el aviso '%s': %s", subject, exc)
            return False
