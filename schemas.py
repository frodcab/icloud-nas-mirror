# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
from pydantic import BaseModel, Field


class TwoFactorCode(BaseModel):
    code: str = Field(..., min_length=6, max_length=8, pattern=r"^\s*\d{6,8}\s*$")


class DeviceRef(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=200)


class Health(BaseModel):
    status: str
    version: str
    auth_state: str
    last_run_status: str | None = None
    last_run_at: str | None = None
    photos: dict | None = None
    auth_disabled: bool = False


class MessageOut(BaseModel):
    ok: bool
    message: str
