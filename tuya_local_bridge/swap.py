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
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    name: str | None

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

    Matching happens in two passes: on ``(domain, name)`` first, then on the
    domain alone where it is unambiguous on both sides. The second pass exists
    because the two integrations do not always name the same thing alike --
    see below.

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
    leftover: list[dict[str, Any]] = []

    def pair(cloud_entity, local_entity) -> None:
        claimed.add(str(local_entity["entity_id"]))
        plan.pairs.append(
            EntityPair(
                cloud_entity_id=str(cloud_entity["entity_id"]),
                local_entity_id=str(local_entity["entity_id"]),
                domain=_key(cloud_entity)[0],
                name=cloud_entity.get("original_name"),
            )
        )

    for entity in cloud:
        candidates = local_by_key.get(_key(entity)) or []
        unclaimed = [c for c in candidates if str(c["entity_id"]) not in claimed]
        if len(unclaimed) == 1:
            pair(entity, unclaimed[0])
        else:
            leftover.append(entity)

    # Second pass, by domain alone. The two integrations do not always agree on
    # a name for the same thing -- Tuya's cloud calls a plug's only switch
    # "Socket 1" while tuya-local leaves the primary entity unnamed -- so
    # matching on the name misses the single most common device there is.
    #
    # Safe only when the domain is unambiguous on both sides: exactly one
    # unmatched cloud entity and exactly one unclaimed local entity. Where a
    # domain holds several of either (a plug's four power sensors, say) there
    # is a real choice to make and guessing would put readings on the wrong
    # sensor, so those are still reported rather than paired.
    by_domain_cloud: dict[str, list[dict[str, Any]]] = {}
    for entity in leftover:
        by_domain_cloud.setdefault(_key(entity)[0], []).append(entity)

    by_domain_local: dict[str, list[dict[str, Any]]] = {}
    for entity in local:
        if str(entity["entity_id"]) not in claimed:
            by_domain_local.setdefault(_key(entity)[0], []).append(entity)

    for entity in leftover:
        domain = _key(entity)[0]
        locals_here = [
            e for e in by_domain_local.get(domain, [])
            if str(e["entity_id"]) not in claimed
        ]
        if len(by_domain_cloud.get(domain, [])) == 1 and len(locals_here) == 1:
            pair(entity, locals_here[0])
        else:
            plan.cloud_unmatched.append(str(entity["entity_id"]))

    plan.local_unmatched = [
        str(e["entity_id"]) for e in local if str(e["entity_id"]) not in claimed
    ]
    plan.cloud_unmatched.sort()
    plan.pairs.sort(key=lambda p: p.cloud_entity_id)
    return plan


def domain_of(entity_id: str) -> str:
    """The bit before the dot: ``switch.hot_water`` -> ``switch``."""
    return str(entity_id).split(".", 1)[0] if "." in str(entity_id) else ""


def candidates_for(plan: SwapPlan, cloud_entity_id: str) -> list[str]:
    """Local entities that could reasonably take ``cloud_entity_id``'s place.

    Same domain, not already spoken for. Crossing domains is not offered
    because Home Assistant would refuse the rename anyway.
    """
    domain = domain_of(cloud_entity_id)
    return [e for e in plan.local_unmatched if domain_of(e) == domain]


def add_manual_pairs(plan: SwapPlan, choices: dict[str, str]) -> dict[str, str]:
    """Fold the user's own pairings into a plan. Returns anything refused.

    The automatic matcher deliberately gives up where a domain holds several
    entities on each side and the names do not correspond -- a plug whose cloud
    switches are "Socket 1" and "Child lock" against local ones called nothing
    and "Overcharge protection". Guessing there would put a switch on the wrong
    relay. But the person looking at the screen knows which is which, so the
    answer is to ask rather than to refuse outright.

    Every choice is still checked: the entities must be the ones actually
    offered, must share a domain, and must not already be paired. A stale form
    submitted twice cannot pair the same entity into two places.
    """
    refused: dict[str, str] = {}
    for cloud_id, local_id in (choices or {}).items():
        if not local_id:
            continue
        if cloud_id not in plan.cloud_unmatched:
            refused[cloud_id] = "no longer needs a pairing"
            continue
        if local_id not in plan.local_unmatched:
            refused[cloud_id] = "that local entity is already spoken for"
            continue
        if domain_of(cloud_id) != domain_of(local_id):
            refused[cloud_id] = "those are different kinds of entity"
            continue

        plan.pairs.append(
            EntityPair(
                cloud_entity_id=cloud_id,
                local_entity_id=local_id,
                domain=domain_of(cloud_id),
                name=None,
            )
        )
        plan.cloud_unmatched.remove(cloud_id)
        plan.local_unmatched.remove(local_id)

    plan.pairs.sort(key=lambda p: p.cloud_entity_id)
    return refused


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
        except Exception as exc:
            logger.exception("could not park %s", pair.cloud_entity_id)
            results.append(SwapResult(pair.cloud_entity_id, "error", str(exc)))
            continue

        try:
            registry.update_entity(
                pair.local_entity_id, new_entity_id=pair.cloud_entity_id
            )
        except Exception as exc:
            logger.exception("could not rename %s; undoing", pair.local_entity_id)
            try:
                registry.update_entity(
                    pair.parked_entity_id,
                    new_entity_id=pair.cloud_entity_id,
                    disabled_by=None,
                )
            except Exception:
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
        except Exception as exc:
            logger.exception("rollback failed for %s", cloud_entity_id)
            results.append(SwapResult(cloud_entity_id, "error", str(exc)))
            continue

        migration.rolled_back_at = _now()
        results.append(SwapResult(cloud_entity_id, "rolled_back", f"back to {original}"))

    return results


def _now() -> float:
    import time

    return time.time()


CLOUD_SUFFIX = " (cloud)"


def device_display_name(device: dict[str, Any]) -> str:
    """What Home Assistant shows for a device: the user's name, else its own."""
    return str(device.get("name_by_user") or device.get("name") or "")


def plan_adoption(cloud_device: dict, local_device: dict) -> dict[str, Any]:
    """What the local device should inherit besides its name.

    A device that has moved rooms is a device you cannot find. Area and labels
    are how Home Assistant groups things for dashboards, voice and automations
    by area, and the converted device arrives with none of it — so it ends up
    in "no area" while the disabled original keeps the room.

    Only what the cloud device actually has, and only where the local one has
    nothing, so a deliberate choice already made is never overwritten.
    """
    changes: dict[str, Any] = {}
    area = cloud_device.get("area_id")
    if area and not local_device.get("area_id"):
        changes["area_id"] = area
    labels = [str(x) for x in (cloud_device.get("labels") or [])]
    if labels and not (local_device.get("labels") or []):
        changes["labels"] = labels
    return changes


def plan_rename(cloud_device: dict, local_device: dict) -> tuple[str, str] | None:
    """(name for the local device, name for the cloud device), or None.

    After an entity swap the local device holds every id that matters while the
    cloud device keeps the familiar name, so two identically named devices sit
    side by side and nothing says which is which. The name follows the ids.

    Returns None when there is nothing worth doing -- no name to move, or it
    has already been moved.
    """
    wanted = device_display_name(cloud_device)
    if not wanted or wanted.endswith(CLOUD_SUFFIX):
        # The suffix is the marker that this has already been done. Matching
        # display names is *not*: tuya-local usually names its device exactly
        # what the cloud integration named its own, so two identical names is
        # the collision this exists to resolve, not evidence it is resolved.
        return None
    return wanted, wanted + CLOUD_SUFFIX


def apply_rename(
    registry,
    store: ProvenanceStore,
    tuya_id: str,
    cloud_device: dict,
    local_device: dict,
) -> str | None:
    """Give the local device the cloud device's name. Returns what it is now.

    The cloud device is renamed first. Two devices may not share a name in any
    useful sense, and leaving the old one holding it while the new one waits is
    the state this exists to end.
    """
    planned = plan_rename(cloud_device, local_device)
    adoption = plan_adoption(cloud_device, local_device)
    if planned is None and not adoption:
        return None
    local_name, cloud_name = planned or (None, None)

    # Record the *user override* rather than the displayed name. Restoring a
    # display name would pin an integration-supplied name as a user override,
    # so undoing would leave the device subtly different from how it started.
    cloud_before = str(cloud_device.get("name_by_user") or "")
    local_before = str(local_device.get("name_by_user") or "")

    if cloud_name is not None:
        registry.update_device(str(cloud_device["id"]), name_by_user=cloud_name)
    try:
        changes: dict[str, Any] = dict(adoption)
        if local_name is not None:
            changes["name_by_user"] = local_name
        registry.update_device(str(local_device["id"]), **changes)
    except Exception:
        # Put the cloud name back rather than leave both devices renamed
        # halfway, which is harder to understand than not having started.
        if cloud_name is not None:
            with suppress(Exception):
                registry.update_device(
                    str(cloud_device["id"]), name_by_user=cloud_before or None
                )
        raise

    store.record_rename(
        tuya_id,
        cloud_device_id=str(cloud_device["id"]),
        local_device_id=str(local_device["id"]),
        cloud_name_before=cloud_before,
        local_name_before=local_before,
        local_area_before=str(local_device.get("area_id") or ""),
        local_labels_before=[str(x) for x in (local_device.get("labels") or [])],
    )
    return local_name or device_display_name(local_device)


def rollback_rename(registry, store: ProvenanceStore, tuya_id: str) -> str | None:
    """Put the device names back. Returns the name restored to the cloud device."""
    record = store.get(tuya_id)
    rename = record.active_rename if record else None
    if rename is None:
        return None

    registry.update_device(
        rename.local_device_id,
        name_by_user=rename.local_name_before or None,
        area_id=rename.local_area_before or None,
        labels=list(rename.local_labels_before),
    )
    registry.update_device(rename.cloud_device_id, name_by_user=rename.cloud_name_before or None)
    rename.rolled_back_at = _now()
    return rename.cloud_name_before


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

    def update_device(self, device_id: str, **changes: Any) -> dict[str, Any]:
        payload = {"type": "config/device_registry/update", "device_id": device_id}
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

    def update_device(self, device_id: str, **changes: Any) -> dict[str, Any]:
        from .ha_ws import command

        payload = {"type": "config/device_registry/update", "device_id": device_id}
        payload.update(changes)
        return command(self.base_url, self.token, payload)
