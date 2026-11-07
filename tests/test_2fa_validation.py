# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Validación del código 2FA frente al cambio de Apple en iOS/macOS 26.4 (409 = código correcto)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyicloud.exceptions import PyiCloudAPIResponseException  # noqa: E402

from services.icloud_client import ICloudAuth  # noqa: E402


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, replies):
        self.replies = replies  # {(verb, sufijo_url): (status, body)}
        self.calls = []

    def request(self, verb, url, json=None, headers=None):
        self.calls.append((verb, url.rsplit("/verify/", 1)[1], json))
        status, body = self.replies[(verb, url.rsplit("/verify/", 1)[1])]
        if status >= 400:
            raise PyiCloudAPIResponseException("err", status, FakeResponse(status, body))
        return FakeResponse(status, body)


class FakePhone:
    push_mode = "sms"

    def as_phone_number_payload(self):
        return {"id": 1}


class FakeApi:
    def __init__(self, method, replies, bridge=None):
        self.two_factor_delivery_method = method
        self._trusted_device_bridge_state = bridge
        self._auth_endpoint = "https://idmsa.apple.com/appleauth/auth"
        self.session = FakeSession(replies)
        self._two_factor_code_requested = True
        self.trusted = False
        self.bridge_validate_called = False

    def _trusted_phone_number(self):
        return FakePhone()

    def _get_auth_headers(self, extra=None):
        return dict(extra or {})

    def _clear_trusted_device_bridge_state(self):
        self._trusted_device_bridge_state = None

    def validate_2fa_code(self, code):
        self.bridge_validate_called = True
        return True

    @property
    def is_trusted_session(self):
        return self.trusted

    def trust_session(self):
        self.trusted = True
        return True

    @property
    def requires_2fa(self):
        return not self.trusted


def make_auth(api, tmp_path):
    auth = ICloudAuth("u@example.com", "pw", tmp_path, False)
    auth.api, auth.state, auth.delivery = api, "needs_2fa", api.two_factor_delivery_method
    return auth


def test_sms_code_accepted_with_409_valid(tmp_path):
    api = FakeApi("sms", {("POST", "phone/securitycode"): (409, {"securityCode": {"valid": True}})})
    ok, msg = make_auth(api, tmp_path).submit_2fa(" 123456 ")
    assert ok, msg
    assert api.trusted and api.session.calls[0][2]["securityCode"] == {"code": "123456"}


def test_wrong_sms_code_reports_apple_answer(tmp_path):
    api = FakeApi("sms", {("POST", "phone/securitycode"): (400, {"service_errors": [{"code": "-21669"}]})})
    auth = make_auth(api, tmp_path)
    ok, msg = auth.submit_2fa("000000")
    assert not ok and "HTTP 400" in msg and "-21669" in msg
    assert auth.state == "needs_2fa" and not api.trusted


def test_trusted_device_falls_back_to_put(tmp_path):
    api = FakeApi("trusted_device", {
        ("POST", "trusteddevice/securitycode"): (405, {}),
        ("PUT", "trusteddevice/securitycode"): (409, {"securityCode": {"valid": True}}),
    })
    ok, msg = make_auth(api, tmp_path).submit_2fa("123456")
    assert ok, msg
    assert [c[0] for c in api.session.calls] == ["POST", "PUT"]


def test_trusted_device_wrong_code_does_not_retry_with_put(tmp_path):
    api = FakeApi("trusted_device", {("POST", "trusteddevice/securitycode"): (400, {"x": 1})})
    ok, _ = make_auth(api, tmp_path).submit_2fa("123456")
    assert not ok and len(api.session.calls) == 1


def test_bridge_flow_delegates_to_pyicloud(tmp_path):
    bridge = type("B", (), {"uses_legacy_trusted_device_verifier": False})()
    api = FakeApi("trusted_device", {}, bridge=bridge)
    ok, _ = make_auth(api, tmp_path).submit_2fa("123456")
    assert ok and api.bridge_validate_called and api.session.calls == []
