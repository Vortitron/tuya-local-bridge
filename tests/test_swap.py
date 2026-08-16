
from tuya_local_bridge.store import ProvenanceStore
from tuya_local_bridge.swap import (
    apply_swap,
    entities_for_device,
    plan_swap,
    rollback,
)


def ent(entity_id, name=None, device_id="dev", disabled=None):
    return {
        "entity_id": entity_id,
        "original_name": name,
        "device_id": device_id,
        "disabled_by": disabled,
    }


# Taken from a real registry: one cloud entity, twelve local ones.
CLOUD = [ent("light.front_porch")]
LOCAL = [
    ent("light.front_porch_local"),
    ent("switch.front_porch_local_dnd", "Do not disturb"),
    ent("text.front_porch_local_scene", "Scene"),
    ent("number.front_porch_local_timer", "Timer", disabled="integration"),
]


class FakeRegistry:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def list_entities(self):
        return CLOUD + LOCAL

    def update_entity(self, entity_id, **changes):
        if entity_id in self.fail_on:
            raise RuntimeError(f"boom: {entity_id}")
        self.calls.append((entity_id, changes))
        return {"entity_id": changes.get("new_entity_id", entity_id)}


def test_pairs_the_primary_entities():
    plan = plan_swap(CLOUD, LOCAL)

    assert len(plan.pairs) == 1
    pair = plan.pairs[0]
    assert pair.cloud_entity_id == "light.front_porch"
    assert pair.local_entity_id == "light.front_porch_local"


def test_local_extras_are_left_alone():
    plan = plan_swap(CLOUD, LOCAL)
    # tuya-local exposes features the cloud never had; they are not touched.
    assert "switch.front_porch_local_dnd" in plan.local_unmatched
    assert "text.front_porch_local_scene" in plan.local_unmatched


def test_disabled_entities_are_skipped():
    plan = plan_swap(CLOUD, LOCAL)
    assert "number.front_porch_local_timer" not in plan.local_unmatched


def test_named_entities_pair_on_their_name():
    cloud = [ent("sensor.plug_power", "Power")]
    local = [ent("sensor.plug_local_power", "Power"), ent("sensor.plug_local_v", "Voltage")]

    plan = plan_swap(cloud, local)

    assert plan.pairs[0].local_entity_id == "sensor.plug_local_power"
    assert plan.local_unmatched == ["sensor.plug_local_v"]


def test_domain_must_match_too():
    # Same name, different domain, is not the same thing.
    plan = plan_swap([ent("sensor.x", "Timer")], [ent("number.y", "Timer")])
    assert plan.pairs == []
    assert plan.cloud_unmatched == ["sensor.x"]


def test_ambiguity_is_reported_not_guessed():
    cloud = [ent("sensor.plug_power", "Power")]
    local = [ent("sensor.a_power", "Power"), ent("sensor.b_power", "Power")]

    plan = plan_swap(cloud, local)

    assert plan.pairs == []
    assert plan.cloud_unmatched == ["sensor.plug_power"]


def test_cloud_entity_with_no_local_counterpart():
    plan = plan_swap([ent("sensor.cloud_only", "Signal")], LOCAL)
    assert plan.cloud_unmatched == ["sensor.cloud_only"]


def test_entities_for_device_filters_by_device():
    entities = [ent("light.a", device_id="d1"), ent("light.b", device_id="d2")]
    assert [e["entity_id"] for e in entities_for_device(entities, "d1")] == ["light.a"]


# ── applying ───────────────────────────────────────────────────────────────


def test_swap_frees_the_id_before_taking_it(tmp_path):
    registry = FakeRegistry()
    store = ProvenanceStore(str(tmp_path / "p.json"))

    results = apply_swap(registry, plan_swap(CLOUD, LOCAL), store, "abc")

    assert [r.status for r in results] == ["swapped"]
    # Cloud parked and disabled first, then local renamed into the freed id.
    assert registry.calls[0] == (
        "light.front_porch",
        {"new_entity_id": "light.front_porch_cloud", "disabled_by": "user"},
    )
    assert registry.calls[1] == (
        "light.front_porch_local",
        {"new_entity_id": "light.front_porch"},
    )


def test_swap_records_provenance_for_rollback(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    apply_swap(FakeRegistry(), plan_swap(CLOUD, LOCAL), store, "abc")

    migration = store.get("abc").active_migration
    assert migration.cloud_entity_id == "light.front_porch"
    assert migration.local_entity_id_original == "light.front_porch_local"


def test_a_half_done_swap_is_put_back(tmp_path):
    # If the local rename fails, the cloud entity must not be left parked and
    # disabled — that would take the device away with nothing replacing it.
    registry = FakeRegistry(fail_on={"light.front_porch_local"})
    store = ProvenanceStore(str(tmp_path / "p.json"))

    results = apply_swap(registry, plan_swap(CLOUD, LOCAL), store, "abc")

    assert results[0].status == "error"
    assert registry.calls[-1] == (
        "light.front_porch_cloud",
        {"new_entity_id": "light.front_porch", "disabled_by": None},
    )
    assert store.get("abc") is None or not store.get("abc").migrations


def test_failure_to_park_leaves_everything_alone(tmp_path):
    registry = FakeRegistry(fail_on={"light.front_porch"})
    store = ProvenanceStore(str(tmp_path / "p.json"))

    results = apply_swap(registry, plan_swap(CLOUD, LOCAL), store, "abc")

    assert results[0].status == "error"
    assert registry.calls == []


# ── rollback ───────────────────────────────────────────────────────────────


def test_rollback_restores_both_ids(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    apply_swap(FakeRegistry(), plan_swap(CLOUD, LOCAL), store, "abc")

    registry = FakeRegistry()
    results = rollback(registry, store, "abc")

    assert [r.status for r in results] == ["rolled_back"]
    assert registry.calls[0] == (
        "light.front_porch",
        {"new_entity_id": "light.front_porch_local"},
    )
    assert registry.calls[1] == (
        "light.front_porch_cloud",
        {"new_entity_id": "light.front_porch", "disabled_by": None},
    )
    assert store.get("abc").active_migration is None


def test_rollback_is_idempotent(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    apply_swap(FakeRegistry(), plan_swap(CLOUD, LOCAL), store, "abc")
    rollback(FakeRegistry(), store, "abc")

    assert rollback(FakeRegistry(), store, "abc") == []


def test_rollback_of_an_unknown_device():
    store = ProvenanceStore("/nonexistent/p.json")
    assert rollback(FakeRegistry(), store, "nope") == []


def test_rollback_survives_the_store_being_reloaded(tmp_path):
    path = str(tmp_path / "p.json")
    store = ProvenanceStore(path)
    apply_swap(FakeRegistry(), plan_swap(CLOUD, LOCAL), store, "abc")
    store.save()

    reloaded = ProvenanceStore(path)
    assert [r.status for r in rollback(FakeRegistry(), reloaded, "abc")] == ["rolled_back"]
