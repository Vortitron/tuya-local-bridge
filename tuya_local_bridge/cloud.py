"""Tuya cloud access over the device-sharing ("QR / user code") flow.

This is the same path Home Assistant's own Tuya integration uses: the user
reads a User Code out of the Smart Life app, scans a QR, and the resulting
session can list their devices *including* ``local_key`` — with no developer
account, no cloud project, and no IoT Core subscription.

The IoT Core route (Access ID + Secret against ``openapi.tuya*.com``) is
deliberately not implemented: its trial expires after about a month and can
only be renewed every six, so anything built on it breaks for most users.

Note on identity
----------------
The login endpoint is ``/v1.0/m/life/home-assistant/qrcode/tokens`` and takes
Home Assistant's client id.  It is a Tuya<->Home Assistant surface, so this
code is intended to run *inside* Home Assistant, as an add-on or custom
component.  Do not call it from unrelated hosted infrastructure.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import requests
from tuya_sharing.customerapi import CustomerApi, CustomerTokenInfo, SharingTokenListener

from .models import CloudDevice

# Home Assistant's device-sharing registration. See module docstring.
HA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
HA_SCHEMA = "haauthorize"

QR_GATEWAY = "https://apigw.iotbing.com"
QR_LOGIN_SCHEME = "tuyaSmart--qrLogin?token={token}"

DEFAULT_TIMEOUT = 60


class TuyaAuthError(RuntimeError):
    """Login could not be completed."""


@dataclass
class QRChallenge:
    """A pending QR login."""

    token: str

    @property
    def payload(self) -> str:
        """The string to encode into the QR image the user scans."""
        return QR_LOGIN_SCHEME.format(token=self.token)


def request_qr(user_code: str) -> QRChallenge:
    """Ask Tuya for a login token bound to ``user_code``.

    The token is short-lived (roughly a couple of minutes) — generate it
    immediately before showing the QR, not ahead of time.
    """
    resp = requests.post(
        f"{QR_GATEWAY}/v1.0/m/life/home-assistant/qrcode/tokens"
        f"?clientid={HA_CLIENT_ID}&usercode={user_code}&schema={HA_SCHEMA}",
        timeout=DEFAULT_TIMEOUT,
    ).json()
    if not resp.get("success"):
        raise TuyaAuthError(f"{resp.get('code')}: {resp.get('msg')}")
    return QRChallenge(token=resp["result"]["qrcode"])


def poll_login(user_code: str, challenge: QRChallenge) -> dict[str, Any] | None:
    """Check whether the QR has been scanned yet.

    Returns the token bundle on success, ``None`` while still waiting.  Tuya
    reports "not scanned yet" and "token expired" with the same ``E0020003``,
    so callers should impose their own deadline.
    """
    resp = requests.get(
        f"{QR_GATEWAY}/v1.0/m/life/home-assistant/qrcode/tokens/{challenge.token}"
        f"?clientid={HA_CLIENT_ID}&usercode={user_code}",
        timeout=DEFAULT_TIMEOUT,
    ).json()
    if not resp.get("success"):
        return None
    result = resp["result"]
    result["t"] = resp.get("t")
    result["user_code"] = user_code
    return result


class _PersistingTokenListener(SharingTokenListener):
    """Write refreshed tokens straight back to disk.

    The access token lives ~2h; the refresh token is long-lived but is rotated
    on use.  Dropping a rotated refresh token silently ends the session, so
    persistence has to happen at the moment of refresh.
    """

    def __init__(self, session: TuyaCloudSession):
        self._session = session

    def update_token(self, token_info: dict[str, Any]) -> None:
        self._session.token_info.update(token_info)
        self._session.save()


class TuyaCloudSession:
    """An authenticated device-sharing session, persisted to disk."""

    def __init__(self, token_info: dict[str, Any], path: str | None = None):
        self.token_info = dict(token_info)
        self.path = path
        self._api: CustomerApi | None = None

    # ── persistence ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str) -> TuyaCloudSession:
        with open(path) as fh:
            return cls(json.load(fh), path=path)

    def save(self, path: str | None = None) -> None:
        target = path or self.path
        if not target:
            raise ValueError("no path to save session to")
        self.path = target
        directory = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.token_info, fh, indent=2)
            os.replace(tmp, target)
            os.chmod(target, 0o600)  # full control of the user's home
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── api ────────────────────────────────────────────────────────────────

    @property
    def user_code(self) -> str:
        return self.token_info.get("user_code", "")

    @property
    def username(self) -> str:
        return self.token_info.get("username", "")

    @property
    def terminal_id(self) -> str:
        return self.token_info.get("terminal_id", "")

    @property
    def api(self) -> CustomerApi:
        if self._api is None:
            endpoint = self.token_info.get("endpoint")
            if not endpoint:
                raise TuyaAuthError("session has no endpoint; log in again")
            self._api = CustomerApi(
                CustomerTokenInfo(self.token_info),
                HA_CLIENT_ID,
                self.user_code,
                endpoint,
                _PersistingTokenListener(self) if self.path else None,
            )
        return self._api

    def homes(self) -> list[dict[str, Any]]:
        return self.api.get("/v1.0/m/life/users/homes").get("result", []) or []

    def devices(self) -> list[CloudDevice]:
        """Every device across every home on the account."""
        out: list[CloudDevice] = []
        seen: set[str] = set()
        for home in self.homes():
            home_id = home.get("ownerId") or home.get("id")
            resp = self.api.get("/v1.0/m/life/ha/home/devices", {"homeId": home_id})
            for item in resp.get("result", []) or []:
                dev = CloudDevice.from_api(item)
                if dev.id and dev.id not in seen:
                    seen.add(dev.id)
                    out.append(dev)
        return out

    def logout(self) -> None:
        """Revoke this terminal server-side."""
        self.api.post(
            "/v1.0/m/token/terminal/expire",
            None,
            {
                "accessToken": self.token_info.get("access_token"),
                "terminalId": self.terminal_id,
            },
        )


def login_interactive(
    user_code: str,
    on_challenge,
    timeout: float = 180.0,
    interval: float = 3.0,
) -> dict[str, Any]:
    """Run a full QR login.

    ``on_challenge(challenge)`` is called once with the :class:`QRChallenge` so
    the caller can render it however it likes (terminal, web page, HA flow).
    """
    challenge = request_qr(user_code)
    on_challenge(challenge)

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = poll_login(user_code, challenge)
        if result is not None:
            return result
        time.sleep(interval)
    raise TuyaAuthError("timed out waiting for the QR code to be scanned")
