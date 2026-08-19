"""Join the cloud device list against LAN discovery."""
from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from .models import CloudDevice, LanDevice, MatchedDevice, Reconciliation

logger = logging.getLogger(__name__)


def reconcile(
    cloud: Iterable[CloudDevice],
    lan: Iterable[LanDevice],
    *,
    already_converted: Collection[str] = (),
    include_unconvertible: bool = False,
) -> Reconciliation:
    """Split cloud and LAN device lists into matched / cloud-only / LAN-only.

    Devices are joined on the Tuya device id, which both sources agree on (the
    LAN broadcast calls it ``gwId``).  It is stable across IP changes, which is
    why we never join on address.

    ``already_converted`` is the set of device ids tuya-local has already been
    set up for.  They must be classified first: adding a device consumes its
    discovery flow, so a converted device is absent from ``lan`` and would
    otherwise be reported as offline — the opposite of the truth.

    Sub-devices are dropped from ``cloud_only`` unless
    ``include_unconvertible`` is set: a Zigbee bulb behind a hub has no local
    key and listing it as "not yet converted" would be a permanent, unfixable
    row in the UI.
    """
    lan_by_id = {d.id: d for d in lan if d.id}
    converted_ids = set(already_converted)
    seen_lan: set[str] = set()

    result = Reconciliation()

    for c in cloud:
        if c.id in converted_ids:
            result.converted.append(c)
            seen_lan.add(c.id)
            continue

        found = lan_by_id.get(c.id)
        if found is not None and c.convertible:
            seen_lan.add(found.id)
            result.matched.append(MatchedDevice(cloud=c, lan=found))
            continue
        if found is not None:
            # On the LAN but unusable (no key / sub-device). Consume it so it
            # is not misreported as an unknown device below.
            seen_lan.add(found.id)
        if include_unconvertible or c.convertible:
            result.cloud_only.append(c)

    result.lan_only = [d for d in lan_by_id.values() if d.id not in seen_lan]

    result.matched.sort(key=lambda m: m.cloud.name.lower())
    result.converted.sort(key=lambda c: c.name.lower())
    result.cloud_only.sort(key=lambda c: c.name.lower())
    result.lan_only.sort(key=lambda d: d.ip)
    return result


def drop_shared_addresses(devices: Iterable[LanDevice]) -> list[LanDevice]:
    """Discard addresses that more than one device claims.

    Two devices cannot share an address, so when several do, the address
    belongs to something in the middle rather than to any of them. That
    happens whenever broadcasts reach Home Assistant through a routed tunnel:
    the far side masquerades them, every device appears to be at the gateway,
    and Home Assistant's discovery -- which never expires what it recorded --
    keeps that address for good.

    On the house this was found on, devices were still carrying the address of
    the VPN box, so a conversion reported "no answer from 192.168.1.16", which
    is true and deeply confusing.

    Dropping the address rather than the device keeps the flow id, which is
    what conversion actually needs; a scan can then supply a real address.
    """
    devices = list(devices)
    claims: dict[str, set[str]] = {}
    for device in devices:
        if device.ip:
            claims.setdefault(device.ip, set()).add(device.id)
    shared = {ip for ip, ids in claims.items() if len(ids) > 1}
    if not shared:
        return devices

    logger.info(
        "ignoring %s claimed by several devices: probably a tunnel gateway, "
        "not a device",
        ", ".join(sorted(shared)),
    )
    return [
        LanDevice(
            id=d.id,
            ip="" if d.ip in shared else d.ip,
            version=d.version,
            product_key=d.product_key,
            raw=d.raw,
        )
        for d in devices
    ]


def merge_lan(*sources: Iterable[LanDevice]) -> list[LanDevice]:
    """Union LAN facts gathered from several discovery sources.

    Neither source is complete, and they fail in opposite directions:

    * Home Assistant's discovery accumulates everything heard since boot, but
      a device's flow is consumed when it is added — so converted devices
      vanish from it.
    * A UDP scan sees everything currently broadcasting, converted devices
      included, but misses anything asleep during the window (battery sensors
      especially) and carries no flow id.

    Measured on one real network: 16 from HA, 15 from a scan, 18 between them.

    Later sources *refine* earlier ones rather than replacing them, so pass HA
    discovery first — the scan then contributes the protocol version, which HA's
    flow does not carry, while the flow id needed to convert survives in ``raw``.
    """
    merged: dict[str, LanDevice] = {}
    for source in sources:
        for device in source:
            if not device.id:
                continue
            existing = merged.get(device.id)
            if existing is None:
                merged[device.id] = device
                continue
            merged[device.id] = LanDevice(
                id=device.id,
                ip=device.ip or existing.ip,
                version=device.version or existing.version,
                product_key=device.product_key or existing.product_key,
                raw={**existing.raw, **device.raw},
            )
    return sorted(merged.values(), key=lambda d: d.ip)
