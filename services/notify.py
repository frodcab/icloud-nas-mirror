# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Email notifications (SMTP + STARTTLS). Without SMTP_USERNAME/SMTP_PASSWORD they are turned off."""

import logging
import smtplib
from email.message import EmailMessage

from core.i18n import translate_line, translate_text

log = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, host: str, port: int, username: str, password: str, to: str, lang: str = "es"):
        self.lang = lang              # NOTIFY_LANG: idioma de asunto y cuerpo (los textos del código están en español)
        self.host, self.port = host, port
        self.username, self.password, self.to = username, password, to
        self.enabled = bool(username and password and to)
        if not self.enabled:
            log.info("E-mail notifications are off (SMTP_USERNAME / SMTP_PASSWORD missing)")

    def send(self, subject: str, body: str, to: str | None = None) -> bool:
        """Never raises: a failing e-mail cannot bring the sync down. True if it was sent."""
        subject, body = translate_line(subject, self.lang), translate_text(body, self.lang)
        if not self.enabled:
            log.info("[notice not sent] %s", subject)
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
            log.info("Notice sent: %s", subject)
            return True
        except Exception as exc:
            log.error("Could not send the notice '%s': %s", subject, exc)
            return False
