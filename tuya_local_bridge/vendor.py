"""Local keys from vendor-branded Tuya apps (LEDVANCE, SYLVANIA, …).

Plenty of "not Tuya" smart devices are Tuya hardware sold under another brand.
They pair only in that brand's own app, which is a Tuya white-label build with
its own app credentials — so the Smart Life device-sharing flow in
:mod:`tuya_local_bridge.cloud` cannot see them at all.  They show up in the
reconciliation as LAN devices no account can explain.

Those apps talk to Tuya's older *mobile app* API at ``a1.tuya**.com/api.json``,
authenticated with an email and password plus a client id/secret baked into the
app.  That API returns ``localKey`` directly, so given the vendor's credentials
we can cover devices the QR flow never could.

This is a different protocol from :mod:`tuya_local_bridge.cloud`: signed query
parameters rather than encrypted bodies, and a password login rather than a QR
scan.  The two providers are deliberately separate.

Protocol details derived from FlagX/ha-ledvance-tuya-resync-localkey (MIT).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .models import CloudDevice

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# Regional endpoints. The right one is wherever the account was created.
REGIONS = {
    "eu": "https://a1.tuyaeu.com/api.json",
    "us": "https://a1.tuyaus.com/api.json",
    "cn": "https://a1.tuyacn.com/api.json",
    "in": "https://a1.tuyain.com/api.json",
}

USER_AGENT = "TY-UA=APP/Android/1.1.6/SDK/null"
# Any stable value works; the API only uses it to identify the "phone".
APP_DEVICE_ID = "5fe5abb36728cce7b9cd2185625edccbd6d9bd787e40"

API_VERSION_FOR_ACTION = {
    "tuya.m.infrared.keydata.get": "2.0",
    "tuya.m.device.sub.list": "1.1",
}
DEFAULT_API_VERSION = "1.0"

# Only these participate in the signature, in sorted order.
KEYS_TO_SIGN = frozenset(
    {
        "a", "v", "lat", "lon", "lang", "deviceId", "imei", "imsi", "appVersion",
        "ttid", "isH5", "h5Token", "os", "clientId", "postData", "time",
        "requestId", "n4h5", "sid", "sp", "et",
    }
)


@dataclass(frozen=True)
class Vendor:
    """A white-label app's identity."""

    key: str
    label: str
    client_id: str
    secret: str


VENDORS: dict[str, Vendor] = {
    "ledvance": Vendor(
        key="ledvance",
        label="LEDVANCE SMART+",
        client_id="fx3fvkvusmw45d7jn8xh",
        secret="A_armptsqyfpxa4ftvtc739ardncett3uy_cgqx3ku34mh5qdesd7fcaru3gx7tyurr",
    ),
    "sylvania": Vendor(
        key="sylvania",
        label="SYLVANIA Smart",
        client_id="creq75hn4vdg5qvrgryp",
        secret="A_ag4xcmp9rjttkj9yf9e8c3wfxry7yr44_wparh3scdv8dc7rrnuegaf9mqmn4snpk",
    ),
}


class VendorAuthError(RuntimeError):
    """The vendor account rejected the login."""


class VendorApiError(RuntimeError):
    """The vendor API returned an error."""


def _mobile_hash(data: str) -> str:
    """Tuya's shuffled MD5, used for the body when signing."""
    digest = hashlib.md5(data.encode("utf-8")).hexdigest()
    return digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]


def _encrypt_password(public_key: str, exponent: str, password: str) -> str:
    """Textbook (unpadded) RSA over the MD5 of the password.

    Tuya really does use raw RSA here.  It needs no crypto library — raw RSA is
    just modular exponentiation — which keeps this module dependency-free.
    """
    modulus, exp = int(public_key), int(exponent)
    message = hashlib.md5(password.encode("utf-8")).hexdigest().encode("utf-8")
    encrypted = pow(int.from_bytes(message, "big"), exp, modulus)

    # The reference implementation emits the minimal big-endian encoding, then
    # prefixes 64 zeros; both halves matter, so keep the shape exactly.
    length = max(1, (encrypted.bit_length() + 7) // 8)
    return "0" * 64 + encrypted.to_bytes(length, "big").hex()


class VendorSession:
    """Authenticated session against one vendor's Tuya app API."""

    def __init__(
        self,
        vendor: Vendor,
        email: str,
        password: str,
        *,
        region: str = "eu",
        country_code: int = 44,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.vendor = vendor
        self.email = email
        self._password = password
        self.endpoint = REGIONS.get(region, REGIONS["eu"])
        self.country_code = country_code
        self.timeout = timeout
        self.session = requests.Session()
        self.sid: Optional[str] = None

    # ── signing / transport ────────────────────────────────────────────────

    def _sign(self, data: dict[str, Any]) -> str:
        parts = []
        for key in sorted(data):
            value = data.get(key)
            if key not in KEYS_TO_SIGN or value is None or not str(value):
                continue
            rendered = _mobile_hash(value) if key == "postData" else str(value)
            parts.append(f"{key}={rendered}")
        to_sign = "||".join(parts)
        return hmac.new(
            self.vendor.secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _call(
        self,
        action: str,
        post_data: Optional[dict[str, Any]] = None,
        *,
        requires_sid: bool = True,
        extra: Optional[dict[str, Any]] = None,
    ) -> Any:
        if requires_sid and not self.sid:
            raise VendorApiError("not logged in")

        body = (
            {"postData": json.dumps(post_data, separators=(",", ":"))}
            if post_data is not None
            else None
        )
        params: dict[str, Any] = {
            "a": action,
            "appVersion": "1.1.6",
            "appRnVersion": "5.14",
            "channel": "oem",
            "deviceId": APP_DEVICE_ID,
            "platform": "Linux",
            "requestId": str(uuid.uuid4()),
            "lang": "en",
            "clientId": self.vendor.client_id,
            "osSystem": "9",
            "os": "Android",
            "timeZoneId": "Europe/London",
            "ttid": f"sdk_tuya@{self.vendor.client_id}",
            "et": "0.0.1",
            "v": API_VERSION_FOR_ACTION.get(action, DEFAULT_API_VERSION),
            "sdkVersion": "3.10.0",
            "time": str(int(time.time())),
            **(extra or {}),
        }
        if requires_sid:
            params["sid"] = self.sid

        params["sign"] = self._sign({**params, **(body or {})})

        response = self.session.post(
            self.endpoint,
            params=params,
            data=body,
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        return self._unwrap(response.json())

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> Any:
        if payload.get("success"):
            return payload.get("result")
        code = payload.get("errorCode")
        message = payload.get("errorMsg") or code
        if code in ("USER_PASSWD_WRONG", "USER_NOT_EXISTS"):
            raise VendorAuthError(str(message))
        if code == "USER_SESSION_INVALID":
            raise VendorApiError("session expired")
        raise VendorApiError(f"{code}: {message}")

    # ── api ────────────────────────────────────────────────────────────────

    def login(self) -> "VendorSession":
        token = self._call(
            "tuya.m.user.email.token.create",
            {"countryCode": self.country_code, "email": self.email},
            requires_sid=False,
        )
        result = self._call(
            "tuya.m.user.email.password.login",
            {
                "countryCode": str(self.country_code),
                "email": self.email,
                "ifencrypt": 1,
                "options": '{"group": 1}',
                "passwd": _encrypt_password(
                    token["publicKey"], token["exponent"], self._password
                ),
                "token": token["token"],
            },
            requires_sid=False,
        )
        self.sid = result["sid"]
        return self

    def devices(self) -> list[CloudDevice]:
        """Every device on the account, with its local key."""
        out: list[CloudDevice] = []
        seen: set[str] = set()

        for group in self._call("tuya.m.location.list") or []:
            group_id = group.get("groupId")
            if group_id is None:
                continue
            listing = self._call(
                "tuya.m.my.group.device.list", extra={"gid": str(group_id)}
            )
            for entry in listing or []:
                device_id = entry.get("devId")
                if not device_id or device_id in seen:
                    continue
                seen.add(device_id)
                # The listing is thin; the per-device call carries localKey.
                info = self._call("tuya.m.device.get", {"devId": device_id}) or {}
                out.append(_to_cloud_device({**entry, **info}))
        return out


def _to_cloud_device(info: dict[str, Any]) -> CloudDevice:
    """Normalise a vendor-API device onto the shared model."""
    return CloudDevice(
        id=str(info.get("devId") or ""),
        name=str(info.get("name") or ""),
        local_key=str(info.get("localKey") or ""),
        product_id=str(info.get("productId") or ""),
        product_name=str(info.get("productName") or ""),
        category=str(info.get("category") or ""),
        uuid=str(info.get("uuid") or ""),
        online=bool(info.get("isOnline")),
        sub=bool(info.get("isSubDev")),
        # This API reports no address at all — unlike the sharing API, which
        # reports a WAN one. Either way the LAN address comes from discovery.
        wan_ip="",
        raw=info,
    )


def fetch_devices(
    vendor_key: str,
    email: str,
    password: str,
    *,
    region: str = "eu",
    country_code: int = 44,
) -> list[CloudDevice]:
    """Log in to a vendor app account and return its devices."""
    vendor = VENDORS.get(vendor_key.lower())
    if vendor is None:
        raise VendorApiError(
            f"unknown vendor '{vendor_key}'; known: {', '.join(sorted(VENDORS))}"
        )
    session = VendorSession(
        vendor, email, password, region=region, country_code=country_code
    )
    session.login()
    return session.devices()
