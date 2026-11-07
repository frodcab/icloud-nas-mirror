# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Uso del almacenamiento y dispositivos (hacer sonar) con objetos con la forma de los de pyicloud 2.7.0."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.account_info import SOUND_SUBJECT, AccountInfo, AccountInfoError, DevicesConfig  # noqa: E402
from services.entries import AuthRequired  # noqa: E402


class Media:
    def __init__(self, key, label, color, usage):
        self.key, self.label, self.color, self.usage_in_bytes = key, label, color, usage


class Usage:
    total_storage_in_bytes = 200 * 2**30
    used_storage_in_bytes = 150 * 2**30
    available_storage_in_bytes = 50 * 2**30
    used_storage_in_percent = 75.0
    quota_over = False


class Storage:
    usage = Usage()
    usages_by_media = {
        "docs": Media("docs", "Documentos", "ffcc00", 10 * 2**30),
        "photos": Media("photos", "Fotos", "ff9500", 130 * 2**30),
        "backup": Media("backup", "Copias", "5856d6", 10 * 2**30),
    }


class Account:
    def __init__(self):
        self._storage = None
        self.fetches = 0

    @property
    def storage(self):
        if not self._storage:
            self.fetches += 1
            self._storage = Storage()
        return self._storage


class Device:
    def __init__(self, dev_id, name, cls, status="200", battery=0.8, sound=True):
        self._c = {"id": dev_id, "name": name, "deviceDisplayName": f"{cls} 15", "deviceClass": cls,
                   "deviceStatus": status, "batteryLevel": battery, "batteryStatus": "NotCharging"}
        self.name = name
        self.sound_available = sound
        self.sounds = []

    def status(self, additional=None):
        return {k: self._c.get(k) for k in ["batteryLevel", "deviceDisplayName", "deviceStatus", "name",
                                             *(additional or [])]}

    def play_sound(self, subject="Find My iPhone Alert"):
        self.sounds.append(subject)


class Manager:
    def __init__(self, devices):
        self._devices = {d._c["id"]: d for d in devices}
        self.stop_event = threading.Event()
        self.refreshes = []

    @property
    def devices(self):
        return self._devices

    def refresh(self, locate=True):
        self.refreshes.append(locate)


class Api:
    def __init__(self):
        self.account = Account()
        self.devices = Manager([
            Device("a+b/c==", "iPhone de Javi", "iPhone"),
            Device("mac1", "MacBook", "MacBook", status="201", battery=0, sound=False),
        ])


class Auth:
    def __init__(self, ok=True):
        self.api, self.ok = Api(), ok

    def require_api(self):
        if not self.ok:
            raise AuthRequired("needs_2fa")
        return self.api


def test_storage_summary_sorted_and_cached():
    auth = Auth()
    info = AccountInfo(auth)
    st = info.storage()
    assert st["used_percent"] == 75.0 and st["total_bytes"] == 200 * 2**30
    assert [m["key"] for m in st["by_media"]] == ["photos", "docs", "backup"] or \
        [m["bytes"] for m in st["by_media"]] == sorted((m["bytes"] for m in st["by_media"]), reverse=True)
    assert st["by_media"][0]["color"] == "#ff9500"
    info.storage()
    assert auth.api.account.fetches == 1          # caché de 10 min
    info.storage(refresh=True)
    assert auth.api.account.fetches == 2          # refresh vacía la caché interna de pyicloud


def test_devices_listing_without_locating_and_monitor_stopped():
    auth = Auth()
    info = AccountInfo(auth)
    devs = info.devices()["devices"]
    assert [d["name"] for d in devs] == ["iPhone de Javi", "MacBook"]   # conectados primero
    assert devs[0]["battery"] == 80 and devs[0]["status"] == "online"
    assert devs[1]["battery"] is None and devs[1]["status"] == "offline" and not devs[1]["sound_available"]
    assert auth.api.devices.stop_event.is_set()
    info.devices(refresh=True)
    assert auth.api.devices.refreshes == [False]   # nunca pide la ubicación


def test_play_sound_with_base64_id():
    auth = Auth()
    info = AccountInfo(auth)
    info.devices()
    assert info.play_sound("a+b/c==") == "iPhone de Javi"
    assert auth.api.devices.devices["a+b/c=="].sounds == [SOUND_SUBJECT]


def test_play_sound_unsupported_or_unknown():
    info = AccountInfo(Auth())
    with pytest.raises(AccountInfoError, match="no admite"):
        info.play_sound("mac1")
    with pytest.raises(AccountInfoError, match="ya no aparece"):
        info.play_sound("no-existe")


def test_needs_session():
    info = AccountInfo(Auth(ok=False))
    with pytest.raises(AccountInfoError, match="sesión"):
        info.storage()
    with pytest.raises(AccountInfoError):
        info.devices()


def _family_api():
    api = Api()
    api.devices = Manager([
        Device("d1", "iPhone de Alice", "iPhone"),
        Device("d2", "MacBook Pro de ALICE", "MacBook"),
        Device("d3", "iPad de Ana", "iPad"),
    ])
    return api


def test_devices_config_generated_with_all_and_fco_visible(tmp_path):
    import json
    auth = Auth()
    auth.api = _family_api()
    cfg = DevicesConfig(tmp_path / "config" / "devices.json", "alice")
    info = AccountInfo(auth, cfg)
    r = info.devices()
    assert [d["name"] for d in r["devices"]] == ["iPhone de Alice", "MacBook Pro de ALICE"]
    assert r["hidden"] == 1
    data = json.loads(cfg.path.read_text())
    assert data["filtro_nombre"] == "alice" and "_ayuda" in data
    assert {k: v["mostrar"] for k, v in data["dispositivos"].items()} == {"d1": True, "d2": True, "d3": False}
    # el usuario lo edita: se respeta en la siguiente consulta, sin reiniciar
    data["dispositivos"]["d3"]["mostrar"] = True
    data["dispositivos"]["d1"]["mostrar"] = False
    cfg.path.write_text(json.dumps(data))
    assert {d["id"] for d in info.devices()["devices"]} == {"d2", "d3"}


def test_new_device_defaults_to_hidden_unless_filter_matches(tmp_path):
    auth = Auth()
    auth.api = _family_api()
    cfg = DevicesConfig(tmp_path / "devices.json", "alice")
    info = AccountInfo(auth, cfg)
    info.devices()
    auth.api.devices._devices["d4"] = Device("d4", "Watch de Ana", "Watch")
    auth.api.devices._devices["d5"] = Device("d5", "Watch de alice", "Watch")
    names = {d["name"] for d in info.devices(refresh=True)["devices"]}
    assert "Watch de alice" in names and "Watch de Ana" not in names


def test_hidden_device_cannot_play_sound(tmp_path):
    auth = Auth()
    auth.api = _family_api()
    info = AccountInfo(auth, DevicesConfig(tmp_path / "devices.json", "alice"))
    info.devices()
    with pytest.raises(AccountInfoError, match="oculto"):
        info.play_sound("d3")
    assert info.play_sound("d1") == "iPhone de Alice"


def test_broken_devices_json_is_reported_not_overwritten(tmp_path):
    auth = Auth()
    auth.api = _family_api()
    path = tmp_path / "devices.json"
    path.write_text("{ esto no es json")
    info = AccountInfo(auth, DevicesConfig(path, "alice"))
    with pytest.raises(AccountInfoError, match="JSON"):
        info.devices()
    assert path.read_text() == "{ esto no es json"
