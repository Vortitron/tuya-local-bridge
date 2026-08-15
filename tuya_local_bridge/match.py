"""Join the cloud device list against LAN discovery."""
from __future__ import annotations

from collections.abc import Collection, Iterable

from .models import CloudDevice, LanDevice, MatchedDevice, Reconciliation


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
