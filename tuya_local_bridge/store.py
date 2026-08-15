"""Durable record of what we learned, and what we did about it.

A local key is *cached cloud state*, not configuration.  Re-pairing a device in
the Smart Life app rotates its key, and nothing tells you: the tuya-local entry
keeps its old key and the device simply stops responding.  That failure is
silent and can surface months later, so every observation is timestamped and
key changes bump a generation counter rather than overwriting quietly.

The store also records *provenance* for migrations — which cloud entity a local
entity replaced — which is what makes rollback possible.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from .models import CloudDevice, LanDevice

SCHEMA_VERSION = 1


def _now(now: Optional[float] = None) -> float:
    """Resolve a timestamp, treating an explicit 0.0 as a real value."""
    return time.time() if now is None else now


@dataclass
class Migration:
    """Record of a cloud -> local entity swap, kept so it can be undone."""

    cloud_entity_id: str
    local_entity_id: str
    migrated_at: float
    # The entity_id the local entity was originally given, before we renamed it
    # into the cloud entity's slot. Needed to put things back.
    local_entity_id_original: str = ""
    rolled_back_at: Optional[float] = None


@dataclass
class DeviceRecord:
    """Everything we know about one device, across time."""

    device_id: str
    name: str = ""
    local_key: str = ""
    key_generation: int = 1
    key_first_seen: float = 0.0
    key_last_confirmed: float = 0.0
    key_rotated_at: Optional[float] = None
    last_lan_ip: str = ""
    last_seen_on_lan: Optional[float] = None
    protocol_version: str = ""
    product_id: str = ""
    category: str = ""
    migrations: list[Migration] = field(default_factory=list)

    @property
    def active_migration(self) -> Optional[Migration]:
        for m in reversed(self.migrations):
            if m.rolled_back_at is None:
                return m
        return None

    def age_of_key(self, now: Optional[float] = None) -> float:
        """Seconds since the cloud last confirmed this key."""
        return _now(now) - (self.key_last_confirmed or 0.0)


class ProvenanceStore:
    """JSON-backed store. Small, human-readable, and safe to hand-edit."""

    def __init__(self, path: str):
        self.path = path
        self.devices: dict[str, DeviceRecord] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            data = json.load(fh)
        if data.get("schema_version", SCHEMA_VERSION) > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer version of tuya-local-bridge"
            )
        for did, raw in (data.get("devices") or {}).items():
            migrations = [Migration(**m) for m in raw.pop("migrations", [])]
            self.devices[did] = DeviceRecord(**raw, migrations=migrations)

    def save(self) -> None:
        """Atomic write — a half-written provenance file loses migration history."""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "devices": {d: asdict(r) for d, r in sorted(self.devices.items())},
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)  # local keys are device credentials
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── observation ────────────────────────────────────────────────────────

    def record_cloud(self, devices: Iterable[CloudDevice], now: Optional[float] = None) -> list[str]:
        """Fold a cloud sync into the store; return ids whose key rotated."""
        now = _now(now)
        rotated: list[str] = []

        for dev in devices:
            rec = self.devices.get(dev.id)
            if rec is None:
                self.devices[dev.id] = DeviceRecord(
                    device_id=dev.id,
                    name=dev.name,
                    local_key=dev.local_key,
                    key_first_seen=now,
                    key_last_confirmed=now,
                    product_id=dev.product_id,
                    category=dev.category,
                )
                continue

            rec.name = dev.name or rec.name
            rec.product_id = dev.product_id or rec.product_id
            rec.category = dev.category or rec.category

            if dev.local_key and dev.local_key != rec.local_key:
                rec.local_key = dev.local_key
                rec.key_generation += 1
                rec.key_rotated_at = now
                rec.key_first_seen = now
                rotated.append(dev.id)

            if dev.local_key:
                rec.key_last_confirmed = now

        return rotated

    def record_lan(self, devices: Iterable[LanDevice], now: Optional[float] = None) -> None:
        """Fold a LAN scan into the store."""
        now = _now(now)
        for dev in devices:
            rec = self.devices.get(dev.id)
            if rec is None:
                rec = self.devices[dev.id] = DeviceRecord(device_id=dev.id)
            rec.last_lan_ip = dev.ip or rec.last_lan_ip
            rec.protocol_version = dev.version or rec.protocol_version
            rec.last_seen_on_lan = now

    # ── migrations ─────────────────────────────────────────────────────────

    def record_migration(
        self,
        device_id: str,
        cloud_entity_id: str,
        local_entity_id: str,
        local_entity_id_original: str = "",
        now: Optional[float] = None,
    ) -> Migration:
        rec = self.devices.get(device_id)
        if rec is None:
            rec = self.devices[device_id] = DeviceRecord(device_id=device_id)
        migration = Migration(
            cloud_entity_id=cloud_entity_id,
            local_entity_id=local_entity_id,
            local_entity_id_original=local_entity_id_original,
            migrated_at=_now(now),
        )
        rec.migrations.append(migration)
        return migration

    def record_rollback(self, device_id: str, now: Optional[float] = None) -> Optional[Migration]:
        rec = self.devices.get(device_id)
        migration = rec.active_migration if rec else None
        if migration is not None:
            migration.rolled_back_at = _now(now)
        return migration

    # ── queries ────────────────────────────────────────────────────────────

    def stale_keys(self, max_age_seconds: float, now: Optional[float] = None) -> list[DeviceRecord]:
        """Migrated devices whose key has not been re-confirmed recently.

        These are the candidates for silent breakage.
        """
        now = _now(now)
        return [
            r
            for r in self.devices.values()
            if r.active_migration is not None and r.age_of_key(now) > max_age_seconds
        ]

    def get(self, device_id: str) -> Optional[DeviceRecord]:
        return self.devices.get(device_id)

    def as_dict(self) -> dict[str, Any]:
        return {d: asdict(r) for d, r in self.devices.items()}
