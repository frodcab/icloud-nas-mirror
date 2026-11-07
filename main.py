# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from core import config
from schemas import DeviceRef, Health, MessageOut, TwoFactorCode
from core.webauth import WebAuth
from services.account_info import AccountInfo, AccountInfoError, DevicesConfig
from services.apps_mirror import AppsMirror
from services.heic_to_jpg import ConversionDB, HeicToJpg
from services.icloud_client import ICloudAuth, ICloudDriveRemote
from services.local_fs import LocalTree
from services.notify import EmailNotifier
from services.photos_db import PhotosDB
from services.photos_engine import PhotosEngine
from services.photos_limits import PhotosLimits
from services.photos_probe import PhotosProbe
from services.photos_remote import ICloudPhotosRemote
from services.photos_service import PhotosSyncService
from services.scheduler import SyncService
from services.progress import Progress
from services.state_db import StateDB
from services.sync_engine import SyncEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("pyicloud").setLevel(logging.WARNING)


class _QuietPolling(logging.Filter):
    """El panel consulta el progreso cada pocos segundos: esas líneas no aportan nada al log."""
    NOISY = ("GET /health", "GET /api/progress", "GET /api/activity", "GET /api/log", "GET /api/status",
             "GET /api/conflicts", "GET /api/photos/")

    def filter(self, record):
        return not any(n in record.getMessage() for n in self.NOISY)


logging.getLogger("uvicorn.access").addFilter(_QuietPolling())

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"app.css": "text/css", "app.js": "text/javascript", "fotos.js": "text/javascript",
                "login.js": "text/javascript",
                "cuenta.js": "text/javascript", "icon.svg": "image/svg+xml", "icon-32.png": "image/png",
                "icon-192.png": "image/png", "icon-512.png": "image/png", "apple-touch-icon.png": "image/png"}
ICONS = {"icon.svg", "icon-32.png", "icon-192.png", "icon-512.png", "apple-touch-icon.png"}
PUBLIC_STATIC = {"app.css", "login.js"} | ICONS   # lo que necesita la página de login antes de entrar
MANIFEST = {
    "name": "iCloud ⇄ NAS", "short_name": "iCloud NAS", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#000000", "theme_color": "#2d9cff", "lang": "es",
    "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}

db = StateDB(config.STATE_DIR / "state.db")
auth = ICloudAuth(config.ICLOUD_USERNAME, config.ICLOUD_PASSWORD, config.SESSION_DIR, config.ICLOUD_CHINA)
local = LocalTree(config.SYNC_DIR, config.TRASH_DIR)
progress = Progress()
account_info = AccountInfo(auth, DevicesConfig(config.CONFIG_DIR / "devices.json", config.DEVICES_FILTER))
remote = ICloudDriveRemote(auth, progress=progress, list_workers=config.LIST_WORKERS)
engine = SyncEngine(db, local, remote, config.EXCLUDES, config.SYNC_FOLDERS, config.MAX_DELETE, progress=progress,
                    renames_file=config.DATA_DIR / "RENOMBRAR.json",
                    confirm_deletes_over=config.CONFIRM_DELETES_OVER)
# Estado propio (apps.db): la copia de las apps no comparte snapshot con la sync bidireccional
apps = AppsMirror(StateDB(config.STATE_DIR / "apps.db"), LocalTree(config.APPS_DIR, config.TRASH_DIR, trash_prefix="Apps"),
                  remote, config.EXCLUDES, config.MAX_DELETE, progress=progress) if config.SYNC_APPS else None
heic = HeicToJpg(ConversionDB(config.STATE_DIR / "heic.db"), config.SYNC_DIR, config.EXCLUDES,
                 config.HEIC_FOLDERS or config.SYNC_FOLDERS, config.HEIC_QUALITY,
                 progress=progress) if config.HEIC_TO_JPG else None
notifier = EmailNotifier(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USERNAME,
                         config.SMTP_PASSWORD, config.NOTIFY_EMAIL_TO)
service = SyncService(
    db=db, auth=auth, engine=engine, local=local, notifier=notifier,
    interval_min=config.SYNC_INTERVAL_MIN, dry_run=config.DRY_RUN, trash_days=config.TRASH_DAYS,
    state_dir=config.STATE_DIR, public_url=config.PUBLIC_URL, progress=progress, apps=apps, heic=heic,
    force_dry_run_files=config.FORCE_DRY_RUN_FILES,
    stall_s=config.SYNC_STALL_MIN * 60, run_max_s=config.SYNC_RUN_MAX_MIN * 60,
    # Fase 0 de iCloud Fotos: lectura una vez por arranque; la prueba de subir/borrar, solo con data/PRUEBA_FOTOS
    photos_probe=PhotosProbe(auth, config.STATE_DIR, config.DATA_DIR / "PRUEBA_FOTOS") if config.PHOTOS_PROBE else None,
)


def _photos_service() -> PhotosSyncService | None:
    """iCloud Fotos ⇄ NAS: hilo propio, misma sesión de Apple (auth) que Drive."""
    if not config.SYNC_PHOTOS:
        return None
    for d in [config.PHOTOS_DIR] + ([config.PHOTOS_HIDDEN_DIR] if config.PHOTOS_HIDDEN and config.PHOTOS_HIDDEN_DIR else []):
        try:
            d.mkdir(parents=True, exist_ok=True)   # con el usuario de la app, dentro del home montado
        except OSError as exc:
            logging.getLogger(__name__).error("No se puede crear %s (%s): ¿está montado el home (HOME_DIR)?", d, exc)
    pdb = PhotosDB(config.STATE_DIR / "photos.db")
    limits = PhotosLimits(max_downloads=config.PHOTOS_MAX_DOWNLOADS, max_bytes=int(config.PHOTOS_MAX_GB * 2**30),
                          max_uploads=config.PHOTOS_MAX_UPLOADS, max_deletes=config.PHOTOS_MAX_DELETES,
                          pause_s=config.PHOTOS_PAUSE_S)
    engine = PhotosEngine(pdb, config.PHOTOS_DIR, config.TRASH_DIR, ICloudPhotosRemote(auth, progress=photos_progress,
                                                     max_file_s=config.PHOTOS_FILE_MAX_MIN * 60),
                          limits, config.EXCLUDES, config.CONFIRM_DELETES_OVER, progress=photos_progress,
                          upload_dir=config.PHOTOS_UPLOAD_DIR, hidden_enabled=config.PHOTOS_HIDDEN,
                          hidden_dir=config.PHOTOS_HIDDEN_DIR, random_pages=config.PHOTOS_RANDOM_PAGES,
                          random_share=config.PHOTOS_RANDOM_SHARE)
    return PhotosSyncService(db=pdb, auth=auth, engine=engine, notifier=notifier,
                             interval_min=config.PHOTOS_INTERVAL_MIN, dry_run=config.PHOTOS_DRY_RUN,
                             progress=photos_progress, public_url=config.PUBLIC_URL,
                             force_dry_run_files=config.PHOTOS_FORCE_DRY_RUN_FILES,
                             stall_s=config.PHOTOS_STALL_MIN * 60, kill_grace_s=config.PHOTOS_KILL_GRACE_MIN * 60,
                             run_max_s=config.PHOTOS_RUN_MAX_MIN * 60)


photos_progress = Progress()
photos = _photos_service()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.start()
    if photos is not None:
        photos.start()
    yield
    # Apagado limpio: se pide parar las vueltas en curso (terminan el fichero actual) en vez de cortarlas
    service.stop()
    service.request_stop()
    if photos is not None:
        photos.stop()
        photos.request_stop()
        for _ in range(80):                      # hasta ~8 s: Docker da 10 antes del SIGKILL
            if not photos.running and not service.running:
                break
            time.sleep(0.1)


app = FastAPI(title="app-integration-icloud-sync", version=config.APP_VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# --- login del panel (core/webauth): sustituye al HTTP Basic -------------------------------------
webauth = WebAuth(
    users_file=config.USERS_FILE, db_path=config.AUTH_DB, secret_key=config.AUTH_SECRET_KEY,
    app_name="iCloud sync (NAS)", login_page=STATIC / "login.html", account_page=STATIC / "cuenta.html",
    notify=notifier.send, trusted_proxies=config.AUTH_TRUSTED_PROXIES,
    allow_networks=config.AUTH_ALLOW_NETWORKS, deny_networks=config.AUTH_DENY_NETWORKS,
    only_networks=config.AUTH_ONLY_NETWORKS, session_idle_hours=config.SESSION_IDLE_HOURS,
    session_max_days=config.SESSION_MAX_DAYS, public_url=config.PUBLIC_URL,
    send_email=notifier.send if notifier.enabled else None,
)
webauth.install(app)
if webauth.users.bootstrap(config.GUI_USER, config.GUI_PASS):
    logging.getLogger(__name__).info("Creado %s con el usuario '%s' (solo el hash)", config.USERS_FILE, config.GUI_USER)
elif not webauth.users.exists():
    logging.getLogger(__name__).error("No hay %s ni GUI_PASS: nadie puede entrar al panel. Crea un usuario con "
                                      "python -m core.webauth usuario-nuevo <nombre>", config.USERS_FILE)
require_user = webauth.require_user


# --- públicos ---------------------------------------------------------------------------------

@app.get("/health", response_model=Health)
def health():
    last = db.recent_runs(1)
    return Health(
        status="ok",
        version=config.APP_VERSION,
        auth_state=auth.state,
        last_run_status=last[0]["status"] if last else None,
        last_run_at=last[0]["finished_at"] if last else None,
        photos=_photos_health(),
    )


def _photos_health() -> dict | None:
    """Resumen público y sin datos personales de Fotos, para vigilarlo desde fuera (herramientas de monitorización)."""
    if photos is None:
        return None
    try:
        last = photos.db.recent_runs(1)
        st = photos.status()
        return {"running": st["running"], "quiet_s": st["quiet_s"], "failing_since": st["failing_since"],
                "last_run_status": last[0]["status"] if last else None,
                "last_run_at": last[0]["finished_at"] if last else None,
                "restarted_by_watchdog": st["restarted_by_watchdog"]}
    except Exception:
        return None


@app.get("/build-info")
def build_info():
    return {"version": config.APP_VERSION, "build_date": config.BUILD_DATE}


# --- panel (requiere sesión con contraseña + TOTP) ----------------------------------------------

@app.get("/")
def index(request: Request):
    return webauth.page_or_login(request, STATIC / "index.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "max-age=604800"})


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/static/{name}")
def static_file(name: str, request: Request):
    if name not in STATIC_FILES:
        raise HTTPException(404, "No encontrado")
    if name not in PUBLIC_STATIC:
        require_user(request)
    return FileResponse(STATIC / name, media_type=STATIC_FILES[name], headers={"Cache-Control": "no-cache"})


@app.get("/api/status", dependencies=[Depends(require_user)])
def status():
    return {
        **service.status(),
        "version": config.APP_VERSION,
        "config": {
            "sync_dir": str(config.SYNC_DIR),
            "apps_dir": str(config.APPS_DIR) if config.SYNC_APPS else "",
            "heic_to_jpg": (f"Sí, calidad {config.HEIC_QUALITY}, en "
                            f"{', '.join(config.HEIC_FOLDERS) or 'todas las carpetas'}") if config.HEIC_TO_JPG else "",
            "sync_folders": config.SYNC_FOLDERS or ["(todo iCloud Drive)"],
            "excludes": config.EXCLUDES,
            "interval_min": config.SYNC_INTERVAL_MIN,
            "dry_run": config.DRY_RUN,
            "max_delete": config.MAX_DELETE,
            "confirm_deletes_over": config.CONFIRM_DELETES_OVER,
            "trash_days": config.TRASH_DAYS,
            "email_notifications": notifier.enabled,
            "email_to": config.NOTIFY_EMAIL_TO if notifier.enabled else "",
            "icloud_user": config.ICLOUD_USERNAME,
        },
        "snapshot_entries": db.snapshot_count(),
        "open_conflicts": len(db.conflicts()),
        "runs": db.recent_runs(20),
    }


@app.get("/api/progress", dependencies=[Depends(require_user)])
def get_progress():
    return {**progress.snapshot(), **service.status()}


@app.get("/api/activity", dependencies=[Depends(require_user)])
def get_activity(since: int = 0):
    return progress.activity(since)


@app.post("/api/auth/2fa", response_model=MessageOut, dependencies=[Depends(require_user)])
def submit_2fa(body: TwoFactorCode):
    ok, message = auth.submit_2fa(body.code)
    if ok:
        service.trigger()
    return MessageOut(ok=ok, message=message)


@app.post("/api/auth/relogin", response_model=MessageOut, dependencies=[Depends(require_user)])
def relogin():
    if service.running:
        raise HTTPException(409, "Hay una sync en marcha; espera a que termine")
    state = auth.connect()
    if state == "ok":
        service.trigger()
    return MessageOut(ok=state in ("ok", "needs_2fa"), message=f"{state}. {auth.message}".strip())


@app.post("/api/sync/run", response_model=MessageOut, dependencies=[Depends(require_user)])
def run_now(dry_run: bool = False):
    if not service.trigger(dry_run=dry_run):
        return MessageOut(ok=False, message="Ya hay una sincronización en marcha")
    return MessageOut(ok=True, message="Simulación lanzada" if dry_run else "Sincronización lanzada")


@app.post("/api/sync/stop", response_model=MessageOut, dependencies=[Depends(require_user)])
def stop_run():
    if not service.request_stop():
        return MessageOut(ok=False, message="No hay ninguna sincronización en marcha")
    return MessageOut(ok=True, message="Parando al terminar el fichero actual…")


@app.get("/api/conflicts", dependencies=[Depends(require_user)])
def conflicts(all: bool = False):
    return db.conflicts(include_resolved=all)


@app.get("/api/account/storage", dependencies=[Depends(require_user)])
def account_storage(refresh: bool = False):
    try:
        return account_info.storage(refresh=refresh)
    except AccountInfoError as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/devices", dependencies=[Depends(require_user)])
def list_devices(refresh: bool = False):
    try:
        return account_info.devices(refresh=refresh)
    except AccountInfoError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/devices/play-sound", response_model=MessageOut, dependencies=[Depends(require_user)])
def play_sound(body: DeviceRef):
    # El id va en el cuerpo: los de Find My son tipo base64 y pueden llevar "/" o "+"
    try:
        name = account_info.play_sound(body.device_id)
    except AccountInfoError as exc:
        return MessageOut(ok=False, message=str(exc))
    return MessageOut(ok=True, message=f"🔊 Sonando en {name}. Apple te enviará un email de aviso.")


@app.get("/api/pending-deletes", dependencies=[Depends(require_user)])
def pending_deletes(limit: int = 500):
    summary = service.pending_summary()
    if summary is None:
        return {"batch_id": None, "total": 0}
    summary["items"] = summary["items"][:max(0, limit)]
    return summary


def _decide(batch_id: str, decision: str) -> MessageOut:
    n = db.decide_batch(batch_id, decision)
    if not n:
        return MessageOut(ok=False, message="Ese lote ya no está vigente (ha cambiado o ya se decidió). "
                                            "Recarga el panel y revísalo de nuevo.")
    what = "confirmado" if decision == "confirmed" else "descartado"
    started = service.trigger()
    return MessageOut(ok=True, message=f"Borrado {what} ({n} elementos). "
                      + ("Se aplica ahora." if started else "Se aplicará al terminar la sync en curso."))


class GroupDecision(BaseModel):
    folder: str
    side: str
    decision: str   # "confirm" | "reject"


@app.post("/api/pending-deletes/{batch_id}/decide", response_model=MessageOut, dependencies=[Depends(require_user)])
def decide_group(batch_id: str, body: GroupDecision):
    """Confirmar o descartar una sola subcarpeta del lote."""
    if body.decision not in ("confirm", "reject"):
        raise HTTPException(400, "decision debe ser confirm o reject")
    decision = "confirmed" if body.decision == "confirm" else "rejected"
    n = service.decide_group(batch_id, body.folder, body.side, decision)
    if not n:
        return MessageOut(ok=False, message="Esa carpeta ya no está en el lote vigente. Recarga el panel.")
    what = "se borrará" if decision == "confirmed" else "se recuperará"
    started = service.trigger()
    return MessageOut(ok=True, message=f"{body.folder or '/'}: {n} elemento(s) {what}. "
                      + ("Se aplica ahora." if started else "Se aplicará al terminar la sync en curso."))


@app.post("/api/pending-deletes/{batch_id}/confirm", response_model=MessageOut, dependencies=[Depends(require_user)])
def confirm_deletes(batch_id: str):
    return _decide(batch_id, "confirmed")


@app.post("/api/pending-deletes/{batch_id}/reject", response_model=MessageOut, dependencies=[Depends(require_user)])
def reject_deletes(batch_id: str):
    return _decide(batch_id, "rejected")


@app.post("/api/conflicts/{conflict_id}/resolve", response_model=MessageOut, dependencies=[Depends(require_user)])
def resolve_conflict(conflict_id: int):
    if not db.resolve_conflict(conflict_id):
        raise HTTPException(404, "Conflicto no encontrado")
    return MessageOut(ok=True, message="Marcado como revisado")


@app.get("/api/log", dependencies=[Depends(require_user)])
def get_log(since: int = 0):
    """Log del run en curso (incremental con ?since=) o, si no hay ninguno, el del último run."""
    if service.running or progress.snapshot()["log_seq"]:
        lines = progress.log_lines(since)
        return {"live": service.running, "seq": lines[-1][0] if lines else since,
                "lines": [line for _, line in lines]}
    path = config.STATE_DIR / "last_run.log"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"live": False, "seq": 0, "lines": text.splitlines()}


# --- iCloud Fotos -----------------------------------------------------------------------------

def _photos() -> PhotosSyncService:
    if photos is None:
        raise HTTPException(404, "La sync de Fotos está desactivada (SYNC_PHOTOS=false)")
    return photos


@app.get("/api/photos/status", dependencies=[Depends(require_user)])
def photos_status():
    p = _photos()
    return {**p.status(), "progress": photos_progress.snapshot(), "runs": p.db.recent_runs(10),
            "config": {"dir": str(config.PHOTOS_DIR), "upload_dir": config.PHOTOS_UPLOAD_DIR,
                       "hidden": config.PHOTOS_HIDDEN,
                       "hidden_dir": str(config.PHOTOS_HIDDEN_DIR or config.PHOTOS_DIR / "Álbumes" / "Oculto"),
                       "interval_min": config.PHOTOS_INTERVAL_MIN, "max_downloads": config.PHOTOS_MAX_DOWNLOADS,
                       "max_gb": config.PHOTOS_MAX_GB, "max_uploads": config.PHOTOS_MAX_UPLOADS,
                       "max_deletes": config.PHOTOS_MAX_DELETES, "confirm_deletes_over": config.CONFIRM_DELETES_OVER}}


@app.post("/api/photos/run", response_model=MessageOut, dependencies=[Depends(require_user)])
def photos_run(dry_run: bool = False):
    if not _photos().trigger(dry_run=dry_run):
        return MessageOut(ok=False, message="Ya hay una sincronización de Fotos en marcha")
    return MessageOut(ok=True, message="Simulación de Fotos lanzada" if dry_run else "Sincronización de Fotos lanzada")


@app.post("/api/photos/stop", response_model=MessageOut, dependencies=[Depends(require_user)])
def photos_stop():
    if not _photos().request_stop():
        return MessageOut(ok=False, message="No hay ninguna sincronización de Fotos en marcha")
    return MessageOut(ok=True, message="Parando Fotos al terminar el fichero actual…")


@app.get("/api/photos/pending-deletes", dependencies=[Depends(require_user)])
def photos_pending(limit: int = 500):
    summary = _photos().pending_summary()
    if summary is None:
        return {"batch_id": None, "total": 0}
    summary["items"] = summary["items"][:max(0, limit)]
    return summary


@app.post("/api/photos/pending-deletes/{batch_id}/decide", response_model=MessageOut,
          dependencies=[Depends(require_user)])
def photos_decide_group(batch_id: str, body: GroupDecision):
    if body.decision not in ("confirm", "reject"):
        raise HTTPException(400, "decision debe ser confirm o reject")
    p = _photos()
    decision = "confirmed" if body.decision == "confirm" else "rejected"
    n = p.decide_group(batch_id, body.folder, body.side, decision)
    if not n:
        return MessageOut(ok=False, message="Esa carpeta ya no está en el lote vigente. Recarga el panel.")
    started = p.trigger()
    what = "se borrará" if decision == "confirmed" else "se recuperará"
    return MessageOut(ok=True, message=f"{body.folder or '/'}: {n} foto(s) {what}. "
                      + ("Se aplica ahora." if started else "Se aplicará al terminar la sync en curso."))


@app.post("/api/photos/pending-deletes/{batch_id}/{decision}", response_model=MessageOut,
          dependencies=[Depends(require_user)])
def photos_decide_batch(batch_id: str, decision: str):
    if decision not in ("confirm", "reject"):
        raise HTTPException(404, "No encontrado")
    p = _photos()
    n = p.db.decide_batch(batch_id, "confirmed" if decision == "confirm" else "rejected")
    if not n:
        return MessageOut(ok=False, message="Ese lote ya no está vigente. Recarga el panel y revísalo de nuevo.")
    started = p.trigger()
    what = "confirmado" if decision == "confirm" else "descartado"
    return MessageOut(ok=True, message=f"Borrado {what} ({n} fotos). "
                      + ("Se aplica ahora." if started else "Se aplicará al terminar la sync en curso."))
