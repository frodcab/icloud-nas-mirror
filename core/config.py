# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Única fuente de variables de entorno de la app (AGENTS.md §3.2)."""

import os
from pathlib import Path

from core import names


def _bool(name: str, default: bool) -> bool:
    return (os.getenv(name) or str(default)).strip().lower() in ("1", "true", "yes", "si", "sí")


def _list(name: str, default: str = "") -> list[str]:
    # Vacío = valor por defecto (el compose pasa "" cuando la variable no está definida)
    return [item.strip() for item in (os.getenv(name) or default).split(",") if item.strip()]


def _load_secrets_file() -> None:
    """Contraseñas desde $DATA_DIR/secrets.env (KEY=valor, fuera de git, 600). Lo lee la app y no un
    env_file del compose: algunos orquestadores lo resuelven fuera del contenedor, donde no se ve el disco
    de los datos. Lo que ya venga en el entorno gana."""
    f = Path(os.getenv("DATA_DIR", "/data")) / "secrets.env"
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.getenv(key):
            os.environ[key] = value


_load_secrets_file()

APP_VERSION = os.getenv("APP_VERSION", "2026.11.12.01")
BUILD_DATE = os.getenv("BUILD_DATE", "")

# Cuenta de Apple. Sin ellas la app arranca (para que /health responda) pero no sincroniza.
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME", "")
ICLOUD_PASSWORD = os.getenv("ICLOUD_PASSWORD", "")
ICLOUD_CHINA = _bool("ICLOUD_CHINA", False)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SYNC_DIR = Path(os.getenv("SYNC_DIR", str(DATA_DIR / "drive")))
SESSION_DIR = DATA_DIR / "session"
STATE_DIR = DATA_DIR / "state"
TRASH_DIR = DATA_DIR / "trash"

# Carpetas de iCloud Drive a sincronizar (rutas relativas a la raíz, separadas por comas).
# Vacío = todo el Drive.
SYNC_FOLDERS = [f.strip("/") for f in _list("SYNC_FOLDERS")]
# Patrones (fnmatch) que se ignoran en los dos lados, comparados con cada componente de la ruta.
EXCLUDES = _list(
    "EXCLUDES",
    ".DS_Store,._*,~$*,*.tmp,.~lock.*,Thumbs.db,desktop.ini,@eaDir,#recycle,#snapshot",
)
SYNC_INTERVAL_MIN = max(1, int(os.getenv("SYNC_INTERVAL_MIN", "15")))
# Carpetas de iCloud que se listan a la vez al recorrer el Drive (1 = de una en una).
# Las descargas y subidas siguen siempre de una en una, para no provocar el throttling de Apple.
LIST_WORKERS = max(1, int(os.getenv("LIST_WORKERS") or "4"))

# App Libraries (Pages, Numbers, Keynote, Atajos...): copia SOLO de iCloud al NAS, fuera de la
# carpeta bidireccional (si no, el motor las subiría a iCloud Drive como una carpeta más).
SYNC_APPS = _bool("SYNC_APPS", True)
APPS_DIR = Path(os.getenv("APPS_DIR") or str(DATA_DIR / "apps"))

# iCloud Fotos ⇄ NAS (services/photos_engine.py), con la misma sesión de Apple que Drive.
# El compose monta el home del usuario (HOME_DIR) en /data/home. Así la carpeta de Fotos y la de las
# ocultas comparten montaje (mover entre ellas es inmediato) y la app crea las carpetas con su usuario
# (si las creara Docker serían de root y no podría escribir).
SYNC_PHOTOS = _bool("SYNC_PHOTOS", True)
PHOTOS_HOME = Path(os.getenv("PHOTOS_HOME") or str(DATA_DIR / "home"))
PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR") or str(PHOTOS_HOME / "Photos" / "iCloud"))
# Fotos ocultas de iCloud: si se bajan (PHOTOS_HIDDEN) y dónde (PHOTOS_HIDDEN_DIR, relativa al home o
# absoluta dentro del contenedor; vacío = PHOTOS_DIR/Álbumes/Oculto)
PHOTOS_HIDDEN = _bool("PHOTOS_HIDDEN", True)
_hidden = (os.getenv("PHOTOS_HIDDEN_DIR") or "").strip()
PHOTOS_HIDDEN_DIR = (Path(_hidden) if _hidden.startswith("/") else PHOTOS_HOME / _hidden) if _hidden else None
PHOTOS_UPLOAD_DIR = os.getenv("PHOTOS_UPLOAD_DIR") or names.UPLOAD_DIR
# Por defecto solo simula: se pasa a real con PHOTOS_DRY_RUN=false en el environment del stack
PHOTOS_DRY_RUN = _bool("PHOTOS_DRY_RUN", True)
# Sonda de solo lectura al arrancar (services/photos_probe.py)
PHOTOS_PROBE = _bool("PHOTOS_PROBE", True)
# Límites por run de la sync de Fotos (services/photos_limits.py), para que Apple no bloquee la cuenta:
# lo que no cabe en un run sale en el siguiente. Con 1000/run cada hora, 200 GB salen en unos 2 días.
PHOTOS_INTERVAL_MIN = max(15, int(os.getenv("PHOTOS_INTERVAL_MIN") or "240"))
# Vigilante: minutos sin señales de vida antes de dar la vuelta por colgada (0 = sin vigilante) y minutos
# de cortesía tras pedir la parada antes de reiniciar el proceso
# Tope de tiempo por fichero (min): pasado esto la descarga se corta, cuenta como fallo y espera antes de reintentarse
PHOTOS_FILE_MAX_MIN = max(1, int(os.getenv("PHOTOS_FILE_MAX_MIN") or "45"))
PHOTOS_STALL_MIN = max(0, int(os.getenv("PHOTOS_STALL_MIN") or "20"))
# Tope de duración de una vuelta de Fotos (min), aunque no deje de llamar a Apple (0 = sin tope)
# Vigilante de Drive (mismo que el de Fotos): minutos sin señales de vida / tope de duración de una vuelta
SYNC_STALL_MIN = max(0, int(os.getenv("SYNC_STALL_MIN") or "45"))
SYNC_RUN_MAX_MIN = max(0, int(os.getenv("SYNC_RUN_MAX_MIN") or "360"))
PHOTOS_RUN_MAX_MIN = max(0, int(os.getenv("PHOTOS_RUN_MAX_MIN") or "180"))
PHOTOS_KILL_GRACE_MIN = max(1, int(os.getenv("PHOTOS_KILL_GRACE_MIN") or "5"))
# Durante la carga inicial: páginas al azar (también de números altos) por vuelta, y qué parte del cupo de
# descargas pueden gastar. 0 = solo el recorrido en orden
PHOTOS_RANDOM_PAGES = max(0, int(os.getenv("PHOTOS_RANDOM_PAGES") or "4"))
PHOTOS_RANDOM_SHARE = min(1.0, max(0.05, float(os.getenv("PHOTOS_RANDOM_SHARE") or "0.3")))
PHOTOS_MAX_DOWNLOADS = max(1, int(os.getenv("PHOTOS_MAX_DOWNLOADS") or "1000"))
PHOTOS_MAX_GB = max(1.0, float(os.getenv("PHOTOS_MAX_GB") or "10"))
PHOTOS_MAX_UPLOADS = max(1, int(os.getenv("PHOTOS_MAX_UPLOADS") or "200"))
PHOTOS_MAX_DELETES = max(1, int(os.getenv("PHOTOS_MAX_DELETES") or "200"))
PHOTOS_PAUSE_S = max(0.0, float(os.getenv("PHOTOS_PAUSE_S") or "0.5"))

# JPG de alta calidad al lado de cada HEIC de la carpeta sincronizada (duplicado, no sustituye).
HEIC_TO_JPG = _bool("HEIC_TO_JPG", True)
HEIC_FOLDERS = [f.strip("/") for f in _list("HEIC_FOLDERS")]   # vacío = todas las de la sync
HEIC_QUALITY = min(100, max(70, int(os.getenv("HEIC_QUALITY") or "95")))

# Dispositivos del panel: data/config/devices.json (se genera solo). DEVICES_FILTER solo se usa al
# crearlo: los dispositivos cuyo nombre lo contenga salen visibles, el resto ocultos.
CONFIG_DIR = DATA_DIR / "config"
# Interruptor de emergencia: si existe este fichero, TODOS los runs son simulaciones (leen iCloud y
# el NAS y apuntan en el log lo que harían, sin cambiar nada). Se crea/borra desde el NAS.
FORCE_DRY_RUN_FILES = [DATA_DIR / "SIMULACION", CONFIG_DIR / "SIMULACION"]
# Solo para Fotos: data/SIMULACION_FOTOS (los de arriba valen para todo)
PHOTOS_FORCE_DRY_RUN_FILES = FORCE_DRY_RUN_FILES + [DATA_DIR / "SIMULACION_FOTOS"]
DEVICES_FILTER = os.getenv("DEVICES_FILTER", "")

# Salvaguardas
# Más de este nº de FICHEROS borrados en una sync → quedan pendientes de tu confirmación en el panel
CONFIRM_DELETES_OVER = max(0, int(os.getenv("CONFIRM_DELETES_OVER") or "5"))
# Solo la copia de las apps (iCloud → NAS): por encima de esto aborta
MAX_DELETE = int(os.getenv("MAX_DELETE", "100"))
TRASH_DAYS = int(os.getenv("TRASH_DAYS", "30"))
DRY_RUN = _bool("DRY_RUN", False)

# Avisos por email. Sin usuario+contraseña, los avisos quedan desactivados.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# Idioma de los correos de aviso (es, en). Los textos del código están en español; se traducen con static/i18n-<idioma>.json
NOTIFY_LANG = (os.getenv("NOTIFY_LANG") or "es").strip().lower()
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "") or SMTP_USERNAME
# Enlace a la UI que se incluye en los correos (p.ej. para meter el código 2FA).
PUBLIC_URL = os.getenv("PUBLIC_URL", "")

# UI/API de administración (HTTP Basic, AGENTS.md §4.3)
# --- Login del panel (core/webauth): usuarios en data/config/usuarios.json, TOTP, bloqueo de IP ---
# GUI_USER/GUI_PASS solo se usan una vez: para crear usuarios.json si no existe (se guarda el hash).
GUI_USER = os.getenv("GUI_USER", "admin")
GUI_PASS = os.getenv("GUI_PASS", "")
USERS_FILE = CONFIG_DIR / "usuarios.json"
AUTH_DB = STATE_DIR / "auth.db"
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS") or "12")
SESSION_MAX_DAYS = float(os.getenv("SESSION_MAX_DAYS") or "30")
# Redes que nunca se bloquean (casa); denegadas siempre; y, si se define, las únicas permitidas
# Panel SIN login, para quien ya lo protege con una VPN o un proxy con autenticación. Apagado por defecto. Solo acepta
# redes privadas salvo que se fije AUTH_ONLY_NETWORKS o se pida AUTH_ALLOW_PUBLIC (bajo tu responsabilidad).
AUTH_DISABLED = _bool("AUTH_DISABLED", False)
AUTH_ALLOW_PUBLIC = _bool("AUTH_ALLOW_PUBLIC", False)
AUTH_ALLOWED_HOSTS = _list("AUTH_ALLOWED_HOSTS")      # nombres de host con los que se abre el panel (anti DNS rebinding)
AUTH_ALLOW_NETWORKS = _list("AUTH_ALLOW_NETWORKS", "192.168.0.0/16")
AUTH_DENY_NETWORKS = _list("AUTH_DENY_NETWORKS")
AUTH_ONLY_NETWORKS = _list("AUTH_ONLY_NETWORKS")
# Proxies de los que se acepta X-Forwarded-For; vacío = 127.0.0.1 + la puerta de enlace del contenedor
AUTH_TRUSTED_PROXIES = _list("AUTH_TRUSTED_PROXIES") or None


def _auth_secret_key() -> str:
    """AUTH_SECRET_KEY (secrets.env) o, si falta, una clave aleatoria guardada en data/config (0600)."""
    key = os.getenv("AUTH_SECRET_KEY", "")
    if key:
        return key
    import secrets as _secrets
    f = CONFIG_DIR / "auth_secret.key"
    if f.exists():
        return f.read_text().strip()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    key = _secrets.token_urlsafe(48)
    f.write_text(key + "\n")
    os.chmod(f, 0o600)
    return key


AUTH_SECRET_KEY = _auth_secret_key()
