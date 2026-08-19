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
from typing import Any, Protocol
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
CONF_SETUP_MODE = "setup_mode"

# Newer tuya-local puts a mode choice in front of the device form. We already
# hold the local key from the cloud, so we want whichever option means "I will
# type the details in" rather than one that goes back to the cloud or restarts
# discovery. Ordered by preference; the first match against the offered
# options wins.
SETUP_MODE_PREFERENCE = ("manual", "manual_entry", "local", "config", "custom")


class FlowError(RuntimeError):
    """The config flow could not be advanced."""


class FlowClient(Protocol):
    """Whatever can POST a step to Home Assistant's config-flow API.

    ``current_step`` is optional: clients that cannot read a flow still work,
    they just have to find out what it wants by being told off for guessing.
    """

    def continue_flow(self, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        ...


TUYA_LOCAL_PORT = 6668


def reachable(host: str, port: int = TUYA_LOCAL_PORT, timeout: float = 2.0) -> bool:
    """Can we open a socket to the device at all?"""
    import socket

    if not host:
        return False
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _flow_response(response: Any, who: str) -> dict[str, Any]:
    """Turn a config-flow HTTP response into a step.

    A 400 here is not a transport failure: it is the flow saying the input
    was wrong, and the body carries the reasons. Raising on it throws away
    the only useful part of the reply — which is how a missing setup_mode
    step surfaced as "Home Assistant returned 400: {...}" instead of being
    handled. Anything else (401, 404, 500) really is a failure.
    """
    if response.status_code == 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("errors"):
            # The error body omits the step type; the caller keys off it.
            return {"type": "form", **body}
    if not response.ok:
        raise FlowError(f"{who} returned {response.status_code}: {response.text[:300]}")
    return response.json()


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
    device_type: str | None = None,
    protocol_version: str | None = None,
    poll_only: bool = False,
) -> ConversionResult:
    """Advance one device's discovery flow as far as it will go.

    Returns ``needs_type`` with ``type_options`` when tuya-local wants a device
    type; call again passing ``device_type``.
    """
    device_input = {
        CONF_DEVICE_ID: device.cloud.id,
        CONF_HOST: device.lan.ip,
        CONF_LOCAL_KEY: device.cloud.local_key,
        CONF_PROTOCOL_VERSION: protocol_version or device.lan.version or "auto",
        CONF_POLL_ONLY: poll_only,
    }
    # Ask the flow what it is waiting for rather than guessing and reading
    # the telling-off. tuya-local grew a mode-selection step in front of the
    # device form, and on that version posting the device fields is rejected
    # wholesale -- "extra keys not allowed" for every one of them. Sometimes
    # that reply names the missing setup_mode and sometimes it says only
    # "base", and a rejection carries no schema, so there is nothing in it to
    # work from. The flow itself will say.
    step = _current_step(client, flow_id)

    if step is not None and _schema_field(step, CONF_SETUP_MODE):
        step = client.continue_flow(flow_id, {CONF_SETUP_MODE: _pick_setup_mode(step)})

    step = client.continue_flow(flow_id, device_input)

    # Older path, and the fallback for a client that cannot read the flow:
    # infer the mode step from the rejection.
    if _wants_setup_mode(step):
        step = client.continue_flow(flow_id, {CONF_SETUP_MODE: _pick_setup_mode(step)})
        step = client.continue_flow(flow_id, device_input)

    # Still rejected as the wrong shape. Say what the form is actually asking
    # for -- "extra keys not allowed" on its own leaves nobody anywhere.
    if _rejected_as_wrong_shape(step):
        wanted = _declared_fields(_current_step(client, flow_id))
        if wanted:
            return ConversionResult(
                device_id=device.cloud.id,
                status="error",
                message=(
                    "tuya-local would not accept the device details. Its form "
                    f"is asking for {', '.join(wanted)}; the bridge sent "
                    f"{', '.join(sorted(device_input))}. This usually means "
                    "tuya-local's setup has changed -- please report it."
                ),
                step=step if isinstance(step, dict) else {},
            )

    result = _interpret(device.cloud.id, step)

    # Nothing more to do, or the key/host were rejected.
    if result.status != "needs_type" or device_type is None:
        return result

    step = client.continue_flow(flow_id, {CONF_TYPE: device_type})
    return _interpret(device.cloud.id, step)



def _current_step(client: FlowClient, flow_id: str) -> dict[str, Any] | None:
    """What is this flow waiting for? ``None`` if we cannot find out."""
    reader = getattr(client, "current_step", None)
    if reader is None:
        return None
    try:
        step = reader(flow_id)
    except Exception:
        # Never let a diagnostic read break the conversion it was meant to help.
        logger.warning("could not read flow %s", flow_id, exc_info=True)
        return None
    return step if isinstance(step, dict) else None


def _declared_fields(step: dict[str, Any] | None) -> list[str]:
    """Field names a form step says it wants."""
    if not isinstance(step, dict):
        return []
    return [
        str(entry["name"])
        for entry in step.get("data_schema") or []
        if isinstance(entry, dict) and entry.get("name")
    ]


def _rejected_as_wrong_shape(step: Any) -> bool:
    """Was the input refused for its shape rather than its contents?

    A wrong local key comes back as a specific complaint. "extra keys not
    allowed" means the form does not have those fields at all, which is a
    different problem and needs a different answer.
    """
    if not isinstance(step, dict) or step.get("type") != "form":
        return False
    for value in (step.get("errors") or {}).values():
        text = " ".join(value) if isinstance(value, list) else str(value or "")
        if "extra keys not allowed" in text:
            return True
    return False


def _wants_setup_mode(step: Any) -> bool:
    """Did this step reject our input because it wanted a mode first?"""
    if not isinstance(step, dict) or step.get("type") != "form":
        return False
    errors = step.get("errors") or {}
    if CONF_SETUP_MODE in errors:
        return True
    # Some versions report it only as a schema mismatch on the other keys.
    base = errors.get("base")
    text = " ".join(base) if isinstance(base, list) else str(base or "")
    if "extra keys not allowed" in text and _schema_field(step, CONF_SETUP_MODE):
        return True
    return False


def _schema_field(step: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in step.get("data_schema") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _pick_setup_mode(step: dict[str, Any]) -> str:
    """Choose the option that means "I will supply the details myself".

    Read the offered options rather than hardcoding a value: the wording is
    tuya-local's to change, and guessing wrong sends the user back into a
    cloud flow they do not need.
    """
    field_def = _schema_field(step, CONF_SETUP_MODE) or {}

    # Home Assistant serialises a select as a nested selector, not a flat
    # "options" list. Checked against a live tuya-local flow, which returns:
    #   {"name": "setup_mode", "selector": {"select": {"options":
    #     ["cloud", "manual", "cloud_fresh_login"]}}}
    # Reading only the top level found nothing and fell through to the
    # hardcoded default, which happened to be right — worth fixing before it
    # is not.
    raw = field_def.get("options")
    if not raw:
        selector = field_def.get("selector") or {}
        for kind in ("select", "radio"):
            if isinstance(selector.get(kind), dict):
                raw = selector[kind].get("options")
                if raw:
                    break

    options: list[str] = []
    for option in raw or []:
        if isinstance(option, str):
            options.append(option)
        elif isinstance(option, dict) and option.get("value"):
            options.append(str(option["value"]))
        elif isinstance(option, (list, tuple)) and option:
            options.append(str(option[0]))

    for wanted in SETUP_MODE_PREFERENCE:
        for option in options:
            if wanted in option.lower():
                return option
    return options[0] if options else SETUP_MODE_PREFERENCE[0]


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
        return _flow_response(response, "VomeHome")

    def current_step(self, flow_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.api_url}/api/v1/instances/{quote(self.instance_id)}"
            f"/ha/config/config_entries/flow/{quote(flow_id)}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        return _flow_response(response, "VomeHome")


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
        return _flow_response(response, "Home Assistant")

    def current_step(self, flow_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/api/config/config_entries/flow/{quote(flow_id)}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        return _flow_response(response, "Home Assistant")
