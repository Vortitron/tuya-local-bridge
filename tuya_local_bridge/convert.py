"""Drive tuya-local's config flow to convert a device to local control.

Converting is not one call.  tuya-local's discovery flow stops at a form that
wants the local key, and once that validates it asks which *device type* to use
(it can rarely tell from the DPs alone).  So a conversion is:

    discovery flow ──local──> select_type ──type──> create_entry

We already hold the flow id from discovery, which means we can continue the
exact flow the user would have clicked, rather than starting a fresh one and
racing the discovery.

The type choice is genuinely ambiguous and belongs to the user, so
:func:`convert` stops and *returns the options* rather than guessing.  Call it
again with ``device_type`` to finish.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import quote

import requests

from .models import MatchedDevice

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60

# tuya-local's config-flow field names.
CONF_DEVICE_ID = "device_id"
CONF_HOST = "host"
CONF_LOCAL_KEY = "local_key"
CONF_PROTOCOL_VERSION = "protocol_version"
CONF_POLL_ONLY = "poll_only"
CONF_DEVICE_CID = "device_cid"
CONF_TYPE = "type"

STEP_SELECT_TYPE = "select_type"


class FlowError(RuntimeError):
    """The config flow could not be advanced."""


class FlowClient(Protocol):
    """Whatever can POST a step to Home Assistant's config-flow API."""

    def continue_flow(self, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass
class ConversionResult:
    """Outcome of one attempt to advance a device's flow."""

    device_id: str
    status: str  # "created" | "needs_type" | "error"
    entry_id: str = ""
    title: str = ""
    type_options: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    message: str = ""
    step: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "created"


def convert(
    client: FlowClient,
    device: MatchedDevice,
    flow_id: str,
    *,
    device_type: Optional[str] = None,
    protocol_version: Optional[str] = None,
    poll_only: bool = False,
) -> ConversionResult:
    """Advance one device's discovery flow as far as it will go.

    Returns ``needs_type`` with ``type_options`` when tuya-local wants a device
    type; call again passing ``device_type``.
    """
    step = client.continue_flow(
        flow_id,
        {
            CONF_DEVICE_ID: device.cloud.id,
            CONF_HOST: device.lan.ip,
            CONF_LOCAL_KEY: device.cloud.local_key,
            CONF_PROTOCOL_VERSION: protocol_version or device.lan.version or "auto",
            CONF_POLL_ONLY: poll_only,
        },
    )
    result = _interpret(device.cloud.id, step)

    # Nothing more to do, or the key/host were rejected.
    if result.status != "needs_type" or device_type is None:
        return result

    step = client.continue_flow(flow_id, {CONF_TYPE: device_type})
    return _interpret(device.cloud.id, step)


def _interpret(device_id: str, step: dict[str, Any]) -> ConversionResult:
    """Turn a raw config-flow step into a :class:`ConversionResult`."""
    if not isinstance(step, dict):
        return ConversionResult(
            device_id=device_id, status="error", message=f"unexpected response: {step!r}"
        )

    step_type = step.get("type")

    if step_type == "create_entry":
        return ConversionResult(
            device_id=device_id,
            status="created",
            entry_id=str(step.get("result") or ""),
            title=str(step.get("title") or ""),
            step=step,
        )

    if step_type == "abort":
        return ConversionResult(
            device_id=device_id,
            status="error",
            message=str(step.get("reason") or "flow aborted"),
            step=step,
        )

    if step_type == "form":
        errors = {k: str(v) for k, v in (step.get("errors") or {}).items()}
        if errors:
            return ConversionResult(
                device_id=device_id,
                status="error",
                errors=errors,
                message="; ".join(f"{k}: {v}" for k, v in errors.items()),
                step=step,
            )
        if step.get("step_id") == STEP_SELECT_TYPE:
            return ConversionResult(
                device_id=device_id,
                status="needs_type",
                type_options=extract_options(step, CONF_TYPE),
                step=step,
            )
        return ConversionResult(
            device_id=device_id,
            status="error",
            message=f"unhandled step '{step.get('step_id')}'",
            step=step,
        )

    return ConversionResult(
        device_id=device_id,
        status="error",
        message=f"unhandled flow type '{step_type}'",
        step=step,
    )


def extract_options(step: dict[str, Any], field_name: str) -> list[str]:
    """Pull selectable values for ``field_name`` out of a serialised data_schema.

    Home Assistant serialises voluptuous schemas loosely and the shape has
    changed over releases — options may be bare strings, ``{value,label}``
    pairs, or nested under a ``select`` selector — so this accepts all of them
    rather than assuming one.  tuya-local orders them best-match first.
    """
    for spec in step.get("data_schema") or []:
        if not isinstance(spec, dict) or spec.get("name") != field_name:
            continue
        options = spec.get("options")
        if options is None:
            selector = spec.get("selector") or {}
            options = (selector.get("select") or {}).get("options")
        return _flatten_options(options)
    return []


def _flatten_options(options: Any) -> list[str]:
    out: list[str] = []
    for option in options or []:
        if isinstance(option, str):
            out.append(option)
        elif isinstance(option, dict) and "value" in option:
            out.append(str(option["value"]))
        elif isinstance(option, (list, tuple)) and option:
            out.append(str(option[0]))
    return out


# ── transports ─────────────────────────────────────────────────────────────


class VomeHomeFlowClient:
    """Continue flows through the VomeHome broker (needs the ha:config scope)."""

    def __init__(
        self,
        instance_id: str,
        token: str,
        api_url: str = "https://vome.io",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.instance_id = instance_id
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def continue_flow(self, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"{self.api_url}/api/v1/instances/{quote(self.instance_id)}"
            f"/ha/config/config_entries/flow/{quote(flow_id)}"
        )
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(user_input),
            timeout=self.timeout,
        )
        if not response.ok:
            raise FlowError(f"VomeHome returned {response.status_code}: {response.text[:300]}")
        return response.json()


class DirectFlowClient:
    """Continue flows straight against Home Assistant."""

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def continue_flow(self, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/api/config/config_entries/flow/{quote(flow_id)}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(user_input),
            timeout=self.timeout,
        )
        if not response.ok:
            raise FlowError(
                f"Home Assistant returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()
