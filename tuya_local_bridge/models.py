"""Value types shared across the bridge.

Two independent sources describe the same device and neither is sufficient
alone: the Tuya cloud knows ``device_id -> local_key`` but reports only the
*WAN* address, while LAN discovery knows ``device_id -> 192.168.x.y`` but has
no key.  Joining them is the whole point of this package.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class CloudDevice:
    """A device as the Tuya cloud describes it.

    ``wan_ip`` is deliberately named: the cloud's ``ip`` field is the public
    address the device last phoned home from, *not* something you can connect
    to.  Feeding it to tuya-local is a common and confusing mistake.
    """

    id: str
    name: str
    local_key: str
    product_id: str = ""
    product_name: str = ""
    category: str = ""
    uuid: str = ""
    online: bool = False
    sub: bool = False
    wan_ip: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "CloudDevice":
        return cls(
            id=item.get("id", ""),
            name=item.get("name", ""),
            local_key=item.get("local_key", "") or "",
            product_id=item.get("product_id", "") or "",
            product_name=item.get("product_name", "") or "",
            category=item.get("category", "") or "",
            uuid=item.get("uuid", "") or "",
            online=bool(item.get("online")),
            sub=bool(item.get("sub")),
            wan_ip=item.get("ip", "") or "",
            raw=item,
        )

    @property
    def convertible(self) -> bool:
        """Whether local control is even theoretically possible.

        Zigbee/BLE sub-devices speak their own protocol to a hub and have no
        individual local key, so they can never be driven by tuya-local.
        """
        return bool(self.local_key) and not self.sub


@dataclass(frozen=True)
class LanDevice:
    """A device seen broadcasting on the local network."""

    id: str
    ip: str
    version: str = ""
    product_key: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class MatchedDevice:
    """A cloud device joined to its LAN presence — ready for tuya-local."""

    cloud: CloudDevice
    lan: LanDevice

    @property
    def id(self) -> str:
        return self.cloud.id

    @property
    def config(self) -> dict[str, str]:
        """Exactly the fields tuya-local's config flow asks for."""
        return {
            "device_id": self.cloud.id,
            "host": self.lan.ip,
            "local_key": self.cloud.local_key,
            "protocol_version": self.lan.version or "auto",
        }


@dataclass
class Reconciliation:
    """The three-way split that drives the UI.

    ``lan_only`` is the interesting bucket: a device broadcasting on the LAN
    that the cloud session cannot explain.  Usually it was re-paired (so any
    cached key is stale) or it belongs to another Tuya account.

    ``converted`` has to be tracked separately because a device that has already
    been added to tuya-local *stops being discoverable* — its config flow is
    consumed.  Without this bucket those devices look identical to ones that
    have gone offline, which is exactly backwards.
    """

    matched: list[MatchedDevice] = field(default_factory=list)
    cloud_only: list[CloudDevice] = field(default_factory=list)
    lan_only: list[LanDevice] = field(default_factory=list)
    converted: list[CloudDevice] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "matched": len(self.matched),
            "cloud_only": len(self.cloud_only),
            "lan_only": len(self.lan_only),
            "converted": len(self.converted),
        }

    def get(self, device_id: str) -> Optional[MatchedDevice]:
        for m in self.matched:
            if m.id == device_id:
                return m
        return None
