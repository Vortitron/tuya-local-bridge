"""Move entity ids from the cloud device onto the local one.

Adding a device to tuya-local mints *new* entities, so every automation,
script, dashboard and scene still points at the cloud entity.  Converting
without fixing that leaves you with working local devices and broken
automations — the worst of both.

The fix is to preserve the ``entity_id``: park the cloud entity somewhere else,
then rename the local entity into the id the cloud one just vacated.  Nothing
that references it needs to change.

Pairing
-------
Home Assistant names the *primary* entity of a device after the device itself,
leaving ``original_name`` empty; auxiliary entities carry their own name.  Real
example — one cloud entity against twelve local ones::

    tuya        light.front_porch              original_name=None
    tuya_local  light.front_porch_local        original_name=None      <- pair
                switch.front_porch_local_...   original_name='Do not disturb'
                text.front_porch_local_scene   original_name='Scene'
                ... nine more, mostly disabled

So entities pair on ``(domain, original_name)``.  tuya-local usually exposes
extras the cloud never had; those have no counterpart and are left alone.
Anything that does not pair unambiguously is reported, never guessed at.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from .store import ProvenanceStore

logger = logging.getLogger(__name__)

PARK_SUFFIX = "_cloud"


class SwapError(RuntimeError):
    """The swap could not be completed."""


class EntityRegistry(Protocol):
    """The two entity-registry operations a swap needs."""

    def list_entities(self) -> list[dict[str, Any]]:
        ...

    def update_entity(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class EntityPair:
    """A cloud entity and the local entity that should take over its id."""

    cloud_entity_id: str
    local_entity_id: str
    domain: str
    name: Optional[str]

    @property
    def parked_entity_id(self) -> str:
        return f"{self.cloud_entity_id}{PARK_SUFFIX}"


@dataclass
class SwapPlan:
    """What would happen, computed before anything is changed."""

    pairs: list[EntityPair] = field(default_factory=list)
    cloud_unmatched: list[str] = field(default_factory=list)
    local_unmatched: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.pairs


@dataclass
class SwapResult:
    entity_id: str
    status: str  # "swapped" | "rolled_back" | "error"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("swapped", "rolled_back")


def entities_for_device(
    entities: Iterable[dict[str, Any]], device_id: str
) -> list[dict[str, Any]]:
    """Registry entries belonging to one Home Assistant device."""
    return [e for e in entities if isinstance(e, dict) and e.get("device_id") == device_id]


def _key(entity: dict[str, Any]) -> tuple[str, str]:
    """(domain, normalised name) — the identity an entity keeps across integrations."""
    entity_id = str(entity.get("entity_id") or "")
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    name = entity.get("original_name") or entity.get("name") or ""
    return domain, str(name).strip().lower()


def plan_swap(
    cloud_entities: Iterable[dict[str, Any]],
    local_entities: Iterable[dict[str, Any]],
    *,
    include_disabled: bool = False,
) -> SwapPlan:
    """Work out which local entity should take over which cloud entity id.

    Disabled entities are skipped by default: tuya-local disables most of its
    auxiliary entities, and renaming something the user never enabled is churn
    with no benefit.
    """
    def usable(entities):
        return [
            e
            for e in entities
            if isinstance(e, dict)
            and e.get("entity_id")
            and (include_disabled or not e.get("disabled_by"))
        ]

    cloud = usable(cloud_entities)
    local = usable(local_entities)

    local_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entity in local:
        local_by_key.setdefault(_key(entity), []).append(entity)

    plan = SwapPlan()
    claimed: set[str] = set()

    for entity in cloud:
        key = _key(entity)
        candidates = local_by_key.get(key) or []
        if len(candidates) != 1:
            # Zero means tuya-local does not expose it; more than one is
            # ambiguous. Either way, guessing would be worse than reporting.
            plan.cloud_unmatched.append(str(entity["entity_id"]))
            continue
        match = candidates[0]
        claimed.add(str(match["entity_id"]))
        plan.pairs.append(
            EntityPair(
                cloud_entity_id=str(entity["entity_id"]),
                local_entity_id=str(match["entity_id"]),
                domain=key[0],
                name=entity.get("original_name"),
            )
        )

    plan.local_unmatched = [
        str(e["entity_id"]) for e in local if str(e["entity_id"]) not in claimed
    ]
    plan.pairs.sort(key=lambda p: p.cloud_entity_id)
    return plan


def apply_swap(
    registry: EntityRegistry,
    plan: SwapPlan,
    store: ProvenanceStore,
    device_id: str,
) -> list[SwapResult]:
    """Carry out ``plan``, recording provenance so it can be undone.

    Order matters: the cloud entity must vacate its id *before* the local one
    can take it, so each pair is two updates. If the second fails the first is
    put back, leaving the pair as it was rather than half-done.
    """
    results: list[SwapResult] = []

    for pair in plan.pairs:
        try:
            registry.update_entity(
                pair.cloud_entity_id,
                new_entity_id=pair.parked_entity_id,
                disabled_by="user",
            )
        except Exception as exc:  # noqa: BLE001 - reported per entity
            logger.exception("could not park %s", pair.cloud_entity_id)
            results.append(SwapResult(pair.cloud_entity_id, "error", str(exc)))
            continue

        try:
            registry.update_entity(
                pair.local_entity_id, new_entity_id=pair.cloud_entity_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not rename %s; undoing", pair.local_entity_id)
            try:
                registry.update_entity(
                    pair.parked_entity_id,
                    new_entity_id=pair.cloud_entity_id,
                    disabled_by=None,
                )
            except Exception:  # noqa: BLE001 - nothing more we can do
                logger.exception("could not restore %s", pair.cloud_entity_id)
            results.append(SwapResult(pair.local_entity_id, "error", str(exc)))
            continue

        store.record_migration(
            device_id,
            cloud_entity_id=pair.cloud_entity_id,
            local_entity_id=pair.cloud_entity_id,
            local_entity_id_original=pair.local_entity_id,
        )
        results.append(
            SwapResult(pair.cloud_entity_id, "swapped", f"was {pair.local_entity_id}")
        )

    return results


def rollback(
    registry: EntityRegistry, store: ProvenanceStore, device_id: str
) -> list[SwapResult]:
    """Undo every live swap for ``device_id``.

    The local entity goes back to the id tuya-local gave it, then the cloud
    entity reclaims its own and is re-enabled.
    """
    record = store.get(device_id)
    if record is None:
        return []

    results: list[SwapResult] = []
    for migration in list(record.migrations):
        if migration.rolled_back_at is not None:
            continue
        cloud_entity_id = migration.cloud_entity_id
        parked = f"{cloud_entity_id}{PARK_SUFFIX}"
        original = migration.local_entity_id_original or f"{cloud_entity_id}_local"

        try:
            registry.update_entity(cloud_entity_id, new_entity_id=original)
            registry.update_entity(
                parked, new_entity_id=cloud_entity_id, disabled_by=None
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("rollback failed for %s", cloud_entity_id)
            results.append(SwapResult(cloud_entity_id, "error", str(exc)))
            continue

        migration.rolled_back_at = _now()
        results.append(SwapResult(cloud_entity_id, "rolled_back", f"back to {original}"))

    return results


def _now() -> float:
    import time

    return time.time()


# ── transports ─────────────────────────────────────────────────────────────


class VomeHomeEntityRegistry:
    """Entity registry through the VomeHome broker."""

    def __init__(self, instance_id: str, token: str, api_url: str = "https://vome.io"):
        self.instance_id = instance_id
        self.token = token
        self.api_url = api_url

    def _command(self, payload: dict[str, Any]) -> Any:
        from .ha_discovery import _unwrap, _vomehome_ws

        result = _vomehome_ws(self.instance_id, self.token, self.api_url, payload)
        unwrapped = _unwrap(result)
        if unwrapped:
            return unwrapped
        # _unwrap only returns lists; single-object replies come back raw.
        while isinstance(result, dict) and "result" in result:
            result = result["result"]
        return result

    def list_entities(self) -> list[dict[str, Any]]:
        result = self._command({"type": "config/entity_registry/list"})
        return result if isinstance(result, list) else []

    def update_entity(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        payload = {"type": "config/entity_registry/update", "entity_id": entity_id}
        payload.update(changes)
        return self._command(payload)


class DirectEntityRegistry:
    """Entity registry straight from Home Assistant over WebSocket."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def list_entities(self) -> list[dict[str, Any]]:
        from .ha_ws import command

        result = command(self.base_url, self.token, {"type": "config/entity_registry/list"})
        return result if isinstance(result, list) else []

    def update_entity(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        from .ha_ws import command

        payload = {"type": "config/entity_registry/update", "entity_id": entity_id}
        payload.update(changes)
        return command(self.base_url, self.token, payload)
