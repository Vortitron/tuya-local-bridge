"""Use Home Assistant's own discovery instead of scanning the network ourselves.

tuya-local already listens for Tuya broadcasts and raises a config flow for
every device it finds, recording the Tuya device id as the flow's ``unique_id``
and the address in ``title_placeholders``.  That is exactly the
``device_id -> ip`` half of the join, already collected, by a component that is
permanently resident on the right network.

Reading it back is strictly better than running our own UDP scan:

* it works **remotely** — no code has to run on the LAN;
* it reflects everything HA has heard since boot, not just a scan window;
* it cannot disagree with what the user sees in the HA UI.

The trade-off is that it only sees devices tuya-local has *already* discovered
and which are still pending — once a device is added, its flow disappears.  For
a full picture, combine this with :mod:`tuya_local_bridge.discovery`.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import requests

from .models import LanDevice

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
TUYA_LOCAL_DOMAIN = "tuya_local"


class HaDiscoveryError(RuntimeError):
    """Home Assistant could not be reached, or refused the request."""


def parse_flows(
    flows: Iterable[dict[str, Any]],
    *,
    handler: str | None = TUYA_LOCAL_DOMAIN,
) -> list[LanDevice]:
    """Turn in-progress config flows into :class:`LanDevice` values.

    A tuya-local discovery flow looks like::

        {"handler": "tuya_local", "step_id": "local",
         "context": {"source": "integration_discovery",
                     "unique_id": "bf1000aa2000bb3000ccd1",
                     "title_placeholders": {"name": "192.168.1.146"}}}

    ``unique_id`` is the Tuya device id and ``title_placeholders.name`` is the
    address.  Flows without both are skipped rather than guessed at.
    """
    devices: list[LanDevice] = []
    for flow in flows or []:
        if not isinstance(flow, dict):
            continue
        if handler and flow.get("handler") != handler:
            continue

        context = flow.get("context") or {}
        device_id = context.get("unique_id") or ""
        placeholders = context.get("title_placeholders") or {}
        ip = placeholders.get("name") or context.get("host") or ""

        if not device_id or not _looks_like_ip(ip):
            logger.debug("skipping flow without an id/address: %r", flow)
            continue

        devices.append(
            LanDevice(
                id=str(device_id),
                ip=str(ip),
                # HA's discovery flow does not carry the protocol version;
                # tuya-local probes for it when the entry is created.
                version="",
                raw=flow,
            )
        )
    return devices


def parse_device_registry(
    entries: Iterable[dict[str, Any]],
    *,
    domain: str = TUYA_LOCAL_DOMAIN,
) -> set[str]:
    """Return the device ids ``domain`` already owns a device for.

    Registry identifiers are ``[domain, id]`` pairs, and tuya-local stores the
    Tuya device id verbatim — which is what lets us tell "already converted"
    apart from "offline".
    """
    converted: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for identifier in entry.get("identifiers") or []:
            # JSON gives lists; HA's own types are tuples.
            if len(identifier) == 2 and identifier[0] == domain and identifier[1]:
                converted.add(str(identifier[1]))
    return converted


def map_devices(
    entries: Iterable[dict[str, Any]],
    *,
    domains: tuple[str, ...] = ("tuya", TUYA_LOCAL_DOMAIN),
) -> dict[str, dict[str, str]]:
    """Map each Tuya device id to the Home Assistant devices representing it.

    A converted device exists twice — once from the cloud integration and once
    from tuya-local — both carrying the same Tuya id in their identifiers. That
    pairing is what makes the entity swap possible.
    """
    mapping: dict[str, dict[str, str]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        for identifier in entry.get("identifiers") or []:
            if len(identifier) != 2:
                continue
            domain, tuya_id = identifier[0], str(identifier[1])
            if domain in domains and tuya_id:
                mapping.setdefault(tuya_id, {})[domain] = str(entry["id"])
    return mapping


def device_registry_vomehome(
    instance_id: str, token: str, api_url: str = "https://vome.io",
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Raw device registry via the broker."""
    payload = _vomehome_ws(
        instance_id, token, api_url, {"type": "config/device_registry/list"}, timeout
    )
    return _unwrap(payload)


def device_registry_direct(base_url: str, token: str) -> list[dict[str, Any]]:
    """Raw device registry straight from Home Assistant."""
    from .ha_ws import command

    return command(base_url, token, {"type": "config/device_registry/list"}) or []


def converted_from_vomehome(
    instance_id: str,
    token: str,
    api_url: str = "https://vome.io",
    *,
    domain: str = TUYA_LOCAL_DOMAIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> set[str]:
    """Device ids already set up in ``domain``, via the VomeHome broker."""
    payload = _vomehome_ws(
        instance_id, token, api_url, {"type": "config/device_registry/list"}, timeout
    )
    return parse_device_registry(_unwrap(payload), domain=domain)


def converted_from_home_assistant(
    base_url: str,
    token: str,
    *,
    domain: str = TUYA_LOCAL_DOMAIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> set[str]:
    """Device ids already set up in ``domain``, straight from Home Assistant.

    The device registry is WebSocket-only — there is no REST equivalent — so
    this is the one call that needs :mod:`tuya_local_bridge.ha_ws`.
    """
    from .ha_ws import command

    result = command(base_url, token, {"type": "config/device_registry/list"}, timeout)
    return parse_device_registry(result or [], domain=domain)


def _vomehome_ws(
    instance_id: str,
    token: str,
    api_url: str,
    command: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    """POST a Home Assistant WebSocket command through the VomeHome broker."""
    url = f"{api_url.rstrip('/')}/api/v1/instances/{quote(instance_id)}/ha/ws/command"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(command),
        timeout=timeout,
    )
    if not response.ok:
        raise HaDiscoveryError(
            f"VomeHome returned {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def from_vomehome(
    instance_id: str,
    token: str,
    api_url: str = "https://vome.io",
    *,
    handler: str | None = TUYA_LOCAL_DOMAIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[LanDevice]:
    """Read discovery flows through the VomeHome broker.

    Uses the ``config_entries/flow/progress`` WebSocket command, which the
    broker classifies read-only.  The REST equivalent is POST-only through the
    broker and returns 405 on GET.
    """
    payload = _vomehome_ws(
        instance_id, token, api_url, {"type": "config_entries/flow/progress"}, timeout
    )
    return parse_flows(_unwrap(payload), handler=handler)


def from_home_assistant(
    base_url: str,
    token: str,
    *,
    handler: str | None = TUYA_LOCAL_DOMAIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[LanDevice]:
    """Read discovery flows straight from Home Assistant's REST API.

    ``token`` is a long-lived access token; inside an add-on use ``SUPERVISOR_TOKEN``
    against ``http://supervisor/core``.
    """
    url = f"{base_url.rstrip('/')}/api/config/config_entries/flow?type=integration"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    if response.status_code in (404, 405):
        # Running as an add-on, requests go through Home Assistant's proxy for
        # Supervisor tokens, and that proxy does not permit GET on the config
        # flow index -- it answers 405 for a path that plainly exists. The
        # WebSocket API is the supported way to read in-progress flows and is
        # available over the same token, so use it rather than reporting a
        # failure the user can do nothing about.
        return _flows_over_websocket(base_url, token, handler=handler, timeout=timeout)
    if not response.ok:
        raise HaDiscoveryError(
            f"Home Assistant returned {response.status_code}: {response.text[:300]}"
        )
    return parse_flows(_unwrap(response.json()), handler=handler)


def _flows_over_websocket(
    base_url: str,
    token: str,
    *,
    handler: str | None = TUYA_LOCAL_DOMAIN,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[LanDevice]:
    """Read in-progress config flows over the WebSocket API."""
    from . import ha_ws

    try:
        result = ha_ws.command(
            base_url,
            token,
            {"type": "config_entries/flow/progress"},
            timeout=timeout,
        )
    except ha_ws.HaWebSocketError as exc:
        raise HaDiscoveryError(
            f"Could not read discovery flows from Home Assistant: {exc}"
        ) from exc
    return parse_flows(_unwrap(result), handler=handler)


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    """Pull the flow list out of whichever envelope it arrived in.

    HA's REST endpoint returns a bare list; the brokered WS command wraps it in
    ``{"result": ...}``, sometimes twice.
    """
    seen = 0
    while isinstance(payload, dict) and "result" in payload and seen < 3:
        payload = payload["result"]
        seen += 1
    return payload if isinstance(payload, list) else []


def _looks_like_ip(value: Any) -> bool:
    parts = str(value or "").split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
