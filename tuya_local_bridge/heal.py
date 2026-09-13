"""Repair tuya-local entries whose configuration has drifted.

A tuya-local entry pins three facts that are not actually constant:

* the **address**, which DHCP can move — a reservation makes that unlikely, not
  impossible, and a lease change breaks the entry silently;
* the **local key**, which rotates whenever the device is re-paired;
* the **protocol version**, which can change across firmware updates.

Both failure modes look identical from Home Assistant: the device simply stops
responding, often months later, with nothing in the log that names the cause.

The repair is the same in both cases — re-submit tuya-local's options flow with
values we can re-derive at any time. Discovery gives the current address and
protocol version; the cloud or vendor account gives the current key. That makes
this a *re-sync* rather than a fix for one specific breakage, which is why the
provenance store timestamps every observation.

This needs Home Assistant's options-flow API. It is not currently routed
through the VomeHome broker, so healing runs in direct mode (an add-on, or
anything with a Home Assistant token).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .convert import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
)
from .models import CloudDevice, LanDevice
from .store import ProvenanceStore

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60

# What tuya-local's options form asks for, used only when the form arrives
# with no serialised schema to read. Notably no device_id: an entry that
# already exists knows which device it is, and offering one is refused.
OPTIONS_FIELDS = (CONF_LOCAL_KEY, CONF_HOST, CONF_PROTOCOL_VERSION)

DRIFT_ADDRESS = "address"
DRIFT_KEY = "local_key"
DRIFT_VERSION = "protocol_version"


class HealError(RuntimeError):
    """The entry could not be repaired."""


class OptionsFlowClient(Protocol):
    """Home Assistant's options-flow API."""

    def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        ...

    def continue_options_flow(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    def reload_entry(self, entry_id: str) -> None:
        ...


@dataclass
class Drift:
    """What has changed for one device since it was configured."""

    device_id: str
    name: str = ""
    entry_id: str = ""
    kinds: list[str] = field(default_factory=list)
    current_ip: str = ""
    known_ip: str = ""
    current_key: str = ""
    known_key: str = ""
    current_version: str = ""

    @property
    def needs_repair(self) -> bool:
        return bool(self.kinds) and bool(self.entry_id)

    def describe(self) -> str:
        bits = []
        if DRIFT_ADDRESS in self.kinds:
            bits.append(f"address {self.known_ip or '?'} -> {self.current_ip}")
        if DRIFT_KEY in self.kinds:
            bits.append("local key rotated")
        if DRIFT_VERSION in self.kinds:
            bits.append(f"protocol -> {self.current_version}")
        return ", ".join(bits)


def detect_drift(
    cloud_devices: Iterable[CloudDevice],
    lan_devices: Iterable[LanDevice],
    store: ProvenanceStore,
    entry_ids: dict[str, str],
    *,
    converted_only: bool = True,
) -> list[Drift]:
    """Compare what we can see now against what the store last recorded.

    The store is the record of what tuya-local was configured with, so a
    difference here is exactly a stale entry.  Devices that were never
    converted are skipped: nothing is configured, so nothing can be stale.
    """
    keys = {d.id: d.local_key for d in cloud_devices if d.local_key}
    names = {d.id: d.name for d in cloud_devices}
    lan = {d.id: d for d in lan_devices if d.id}

    drifts: list[Drift] = []
    for device_id, record in store.devices.items():
        entry_id = entry_ids.get(device_id, "")
        if converted_only and not entry_id:
            continue

        seen = lan.get(device_id)
        drift = Drift(
            device_id=device_id,
            name=names.get(device_id) or record.name,
            entry_id=entry_id,
            known_ip=record.last_lan_ip,
            known_key=record.local_key,
            current_ip=seen.ip if seen else "",
            current_version=seen.version if seen else "",
            current_key=keys.get(device_id, ""),
        )

        if drift.current_ip and drift.current_ip != record.last_lan_ip:
            drift.kinds.append(DRIFT_ADDRESS)
        if drift.current_key and drift.current_key != record.local_key:
            drift.kinds.append(DRIFT_KEY)
        if drift.current_version and drift.current_version != record.protocol_version:
            drift.kinds.append(DRIFT_VERSION)

        if drift.kinds:
            drifts.append(drift)

    drifts.sort(key=lambda d: d.name.lower() or d.device_id)
    return drifts


def options_payload(step: dict[str, Any], drift: Drift) -> dict[str, Any]:
    """Fill in whatever fields the options form actually declares.

    The options form is *not* the config form. tuya-local's asks for a key, a
    host, a protocol and a poll-only flag, and refuses a device id outright --
    "not a valid option at 'device_id'" -- because the device is fixed for an
    entry that already exists. Assuming the config flow's shape here cost a
    release.

    So read the form. Everything it asks for that we know, we supply; anything
    else keeps the default it arrived with, and a protocol version it will not
    accept becomes "auto" rather than a 400.
    """
    from .convert import _declared_fields, extract_options

    fields = _declared_fields(step) or list(OPTIONS_FIELDS)
    defaults = {
        str(entry.get("name")): entry.get("default")
        for entry in (step or {}).get("data_schema") or []
        if isinstance(entry, dict) and entry.get("name")
    }

    known: dict[str, Any] = {
        CONF_DEVICE_ID: drift.device_id,
        CONF_HOST: drift.current_ip or drift.known_ip,
        CONF_LOCAL_KEY: drift.current_key or drift.known_key,
    }

    payload: dict[str, Any] = {}
    for name in fields:
        if name == CONF_PROTOCOL_VERSION:
            offered = extract_options(step, CONF_PROTOCOL_VERSION)
            wanted = drift.current_version or str(defaults.get(name) or "auto")
            payload[name] = wanted if (not offered or wanted in offered) else "auto"
        elif known.get(name):
            payload[name] = known[name]
        elif name in defaults and defaults[name] is not None:
            payload[name] = defaults[name]
    return payload


def repair(client: OptionsFlowClient, drift: Drift) -> str:
    """Re-submit the options flow with current values. Returns a status word.

    Every field the form asks for is supplied rather than only the changed one:
    we can re-derive all of them, and a partial form would leave the rest at
    whatever the form happened to default to.
    """
    if not drift.entry_id:
        raise HealError(f"{drift.device_id}: no config entry to repair")

    started = client.start_options_flow(drift.entry_id)
    flow_id = started.get("flow_id")
    if not flow_id:
        raise HealError(f"{drift.device_id}: options flow did not start ({started!r})")

    user_input = options_payload(started, drift)
    step = client.continue_options_flow(flow_id, user_input)

    step_type = (step or {}).get("type")
    if step_type == "create_entry":
        # Saving the options does not always bring the entry back: an entry
        # sitting in setup_retry keeps retrying on its own slow schedule with
        # the values it failed on, so a repair looked like it had done nothing
        # until somebody reloaded the integration by hand. Ask for the reload.
        reload_entry = getattr(client, "reload_entry", None)
        if callable(reload_entry):
            try:
                reload_entry(drift.entry_id)
            except Exception:
                logger.warning(
                    "%s: repaired, but the entry would not reload",
                    drift.device_id,
                    exc_info=True,
                )
        return "repaired"
    if step_type == "abort":
        raise HealError(f"{drift.device_id}: {step.get('reason') or 'aborted'}")
    errors = (step or {}).get("errors") or {}
    if errors:
        raise HealError(f"{drift.device_id}: {'; '.join(map(str, errors.values()))}")
    raise HealError(f"{drift.device_id}: unexpected step {step_type!r}")


def entry_ids_for_devices(
    device_registry: Iterable[dict[str, Any]],
    *,
    domain: str = "tuya_local",
) -> dict[str, str]:
    """Map each Tuya device id to the config entry tuya-local set up for it."""
    mapping: dict[str, str] = {}
    for entry in device_registry or []:
        if not isinstance(entry, dict):
            continue
        entries = entry.get("config_entries") or []
        if not entries:
            continue
        for identifier in entry.get("identifiers") or []:
            if len(identifier) == 2 and identifier[0] == domain and identifier[1]:
                mapping[str(identifier[1])] = str(entries[0])
    return mapping


# ── transport ──────────────────────────────────────────────────────────────


class DirectOptionsFlowClient:
    """Options flow straight against Home Assistant."""

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body),
            timeout=self.timeout,
        )
        if not response.ok:
            raise HealError(f"Home Assistant returned {response.status_code}: {response.text[:300]}")
        return response.json()

    def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        return self._post("/api/config/config_entries/options/flow", {"handler": entry_id})

    def reload_entry(self, entry_id: str) -> None:
        self._post(f"/api/config/config_entries/entry/{quote(entry_id)}/reload", {})

    def continue_options_flow(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        return self._post(
            f"/api/config/config_entries/options/flow/{quote(flow_id)}", user_input
        )
