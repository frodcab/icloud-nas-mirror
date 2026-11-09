# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Datos de la cuenta de iCloud para el panel: uso del almacenamiento y dispositivos (con "hacer sonar").

Usa la misma sesión que la sync. Las respuestas se cachean unos minutos: el panel se consulta a
menudo y no hace falta preguntar a Apple cada vez.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from services.entries import AuthRequired

log = logging.getLogger(__name__)

STORAGE_TTL_S = 10 * 60
DEVICES_TTL_S = 5 * 60
SOUND_SUBJECT = "Buscar desde el NAS"

# deviceStatus de Find My
DEVICE_STATUS = {"200": "online", "201": "offline", "203": "pending", "204": "unregistered"}


class AccountInfoError(Exception):
    """Apple no ha devuelto los datos (sesión caducada, servicio no disponible...)."""


DEVICES_HELP = (
    "Which devices appear in the panel (and can be made to ring). Set \"mostrar\" to true or false. "
    "New devices are added automatically: with mostrar=true if their name contains \"filtro_nombre\" "
    "(case-insensitive; empty = none) and false otherwise. It is re-read on every request."
)


class DevicesConfig:
    """data/config/devices.json: qué dispositivos se muestran. Se genera con todos la primera vez."""

    def __init__(self, path: Path, default_filter: str = ""):
        self.path = path
        self.default_filter = default_filter

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"_ayuda": DEVICES_HELP, "filtro_nombre": self.default_filter, "dispositivos": {}}
        except (OSError, ValueError) as exc:
            # Un JSON mal editado no debe dejar ver todos los dispositivos ni borrar el fichero
            raise ValueError(f"{self.path} no es un JSON válido ({exc}); corrígelo") from exc
        data.setdefault("filtro_nombre", self.default_filter)
        data.setdefault("dispositivos", {})
        return data

    def apply(self, devices: list[dict]) -> tuple[list[dict], int]:
        """Añade los dispositivos que falten, guarda si cambió y devuelve (visibles, nº ocultos)."""
        data = self.load()
        known = data["dispositivos"]
        word = (data.get("filtro_nombre") or "").strip().lower()
        changed = not self.path.exists()
        for d in devices:
            if d["id"] not in known:
                known[d["id"]] = {"nombre": d["name"], "modelo": d["model"],
                                  "mostrar": bool(word) and word in d["name"].lower()}
                changed = True
        if changed:
            self._save(data)
        visible = [d for d in devices if known.get(d["id"], {}).get("mostrar") is True]
        return visible, len(devices) - len(visible)

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


class AccountInfo:
    def __init__(self, auth, devices_config: DevicesConfig | None = None):
        self.auth = auth
        self.devices_config = devices_config
        self._lock = threading.Lock()
        self._storage: tuple[float, dict] | None = None
        self._devices: tuple[float, list[dict]] | None = None
        self._device_objs: dict[str, object] = {}

    def _api(self):
        try:
            return self.auth.require_api()
        except AuthRequired as exc:
            raise AccountInfoError("La sesión de Apple no está activa; verifica el código primero") from exc

    # --- almacenamiento -------------------------------------------------------------------------

    def storage(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._storage and not refresh and time.time() - self._storage[0] < STORAGE_TTL_S:
                return self._storage[1]
            api = self._api()
            try:
                account = api.account
                account._storage = None   # pyicloud lo cachea para siempre en la instancia
                st = account.storage
                usage = st.usage
                total = usage.total_storage_in_bytes
                data = {
                    "total_bytes": total,
                    "used_bytes": usage.used_storage_in_bytes,
                    "available_bytes": usage.available_storage_in_bytes,
                    "used_percent": usage.used_storage_in_percent if total else 0,
                    "over_quota": bool(usage.quota_over),
                    "by_media": sorted(
                        ({"key": m.key, "label": m.label, "color": f"#{m.color}", "bytes": m.usage_in_bytes}
                         for m in st.usages_by_media.values()),
                        key=lambda m: -m["bytes"],
                    ),
                    "fetched_at": time.time(),
                }
            except AccountInfoError:
                raise
            except Exception as exc:
                log.warning("Could not read the iCloud storage: %s", exc)
                raise AccountInfoError(f"Apple no ha devuelto el almacenamiento: {type(exc).__name__}: {exc}") from exc
            self._storage = (time.time(), data)
            return data

    # --- dispositivos ---------------------------------------------------------------------------

    def devices(self, refresh: bool = False) -> dict:
        """{"devices": [visibles], "hidden": nº ocultos por devices.json, "config_file": ruta}"""
        items = self._all_devices(refresh)
        if self.devices_config is None:
            return {"devices": items, "hidden": 0, "config_file": ""}
        try:
            visible, hidden = self.devices_config.apply(items)
        except ValueError as exc:
            raise AccountInfoError(str(exc)) from exc
        return {"devices": visible, "hidden": hidden, "config_file": str(self.devices_config.path)}

    def _all_devices(self, refresh: bool = False) -> list[dict]:
        with self._lock:
            if self._devices and not refresh and time.time() - self._devices[0] < DEVICES_TTL_S:
                return self._devices[1]
            api = self._api()
            try:
                manager = api.devices            # la primera vez carga la lista de Find My
                if self._devices is not None:
                    manager.refresh(locate=False)  # sin pedir la ubicación a los dispositivos
                # pyicloud deja un hilo refrescando Find My cada 5 min: aquí no hace falta
                manager.stop_event.set()
                objs = list(manager.devices.values())
            except AccountInfoError:
                raise
            except Exception as exc:
                log.warning("Could not read the device list: %s", exc)
                raise AccountInfoError(f"Apple no ha devuelto los dispositivos: {type(exc).__name__}: {exc}") from exc
            items = []
            self._device_objs = {}
            for dev in objs:
                st = dev.status(["batteryStatus", "deviceClass", "deviceModel", "id", "lowPowerMode"])
                dev_id = st.get("id") or dev.name
                self._device_objs[dev_id] = dev
                battery = st.get("batteryLevel")
                items.append({
                    "id": dev_id,
                    "name": st.get("name") or dev.name,
                    "model": st.get("deviceDisplayName") or "",
                    "device_class": st.get("deviceClass") or "",
                    "battery": round(battery * 100) if isinstance(battery, (int, float)) and battery > 0 else None,
                    "battery_status": st.get("batteryStatus") or "",
                    "low_power": bool(st.get("lowPowerMode")),
                    "status": DEVICE_STATUS.get(str(st.get("deviceStatus")), "unknown"),
                    "sound_available": bool(dev.sound_available),
                })
            items.sort(key=lambda d: (d["status"] != "online", d["name"].lower()))
            self._devices = (time.time(), items)
            return items

    def play_sound(self, device_id: str) -> str:
        with self._lock:
            dev = self._device_objs.get(device_id)
        if dev is None:
            self._all_devices(refresh=True)
            dev = self._device_objs.get(device_id)
        if dev is None:
            raise AccountInfoError("Ese dispositivo ya no aparece en Buscar")
        if self.devices_config is not None:
            shown = {d["id"] for d in self.devices(refresh=False)["devices"]}
            if device_id not in shown:
                raise AccountInfoError(f"{dev.name} está oculto en {self.devices_config.path}")
        if not dev.sound_available:
            raise AccountInfoError(f"{dev.name} no admite hacer sonar un sonido")
        self._api()
        try:
            dev.play_sound(subject=SOUND_SUBJECT)
        except Exception as exc:
            log.warning("Could not make %s ring: %s", dev.name, exc)
            raise AccountInfoError(f"Apple no ha aceptado la petición: {type(exc).__name__}: {exc}") from exc
        log.info("Sonido enviado a %s", dev.name)
        return dev.name
