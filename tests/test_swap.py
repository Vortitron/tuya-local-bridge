
import pytest

from tuya_local_bridge.store import ProvenanceStore
from tuya_local_bridge.swap import (
    apply_swap,
    entities_for_device,
    plan_swap,
    rollback,
    suggest_predecessors,
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


class TestNamesThatDoNotAgree:
    """The two integrations do not always call the same thing the same name.

    Tuya's cloud names a plug's only switch "Socket 1"; tuya-local leaves the
    primary entity unnamed. Matching on the name alone therefore missed the
    single most common device there is, and reported "no entries to match
    with" for a plug that plainly had one.
    """

    def test_a_plugs_switch_pairs_despite_the_different_name(self):
        cloud = [ent("switch.smart_plug_2_socket_1", "Socket 1")]
        local = [ent("switch.hot_water_local")]

        plan = plan_swap(cloud, local)

        assert len(plan.pairs) == 1
        assert plan.pairs[0].cloud_entity_id == "switch.smart_plug_2_socket_1"
        assert plan.pairs[0].local_entity_id == "switch.hot_water_local"

    def test_an_exact_name_match_still_wins_over_the_domain_fallback(self):
        # Two switches on each side: names must decide, not order.
        cloud = [ent("switch.a_power", "Power"), ent("switch.a_lock", "Child lock")]
        local = [ent("switch.b_lock", "Child lock"), ent("switch.b_power", "Power")]

        plan = plan_swap(cloud, local)

        by_cloud = {p.cloud_entity_id: p.local_entity_id for p in plan.pairs}
        assert by_cloud["switch.a_power"] == "switch.b_power"
        assert by_cloud["switch.a_lock"] == "switch.b_lock"

    def test_several_in_one_domain_is_still_left_alone(self):
        # A plug's four power sensors: guessing would put readings on the wrong
        # one, which is worse than saying nothing.
        cloud = [ent("sensor.a_current", "Current"), ent("sensor.a_power", "Power")]
        local = [ent("sensor.b_one", "Amperage"), ent("sensor.b_two", "Wattage")]

        plan = plan_swap(cloud, local)

        assert plan.pairs == []
        assert sorted(plan.cloud_unmatched) == ["sensor.a_current", "sensor.a_power"]

    def test_the_fallback_does_not_cross_domains(self):
        plan = plan_swap([ent("switch.a", "Socket 1")], [ent("light.b")])
        assert plan.pairs == []
        assert plan.cloud_unmatched == ["switch.a"]

    def test_a_named_pair_and_an_unnamed_pair_in_one_device(self):
        # The real shape of a plug: the switch named differently, the power
        # sensor named the same.
        cloud = [ent("switch.plug_socket_1", "Socket 1"), ent("sensor.plug_power", "Power")]
        local = [ent("switch.plug_local"), ent("sensor.plug_local_power", "Power")]

        plan = plan_swap(cloud, local)

        by_cloud = {p.cloud_entity_id: p.local_entity_id for p in plan.pairs}
        assert by_cloud["switch.plug_socket_1"] == "switch.plug_local"
        assert by_cloud["sensor.plug_power"] == "sensor.plug_local_power"
        assert plan.cloud_unmatched == []

    def test_a_cloud_entity_with_no_local_domain_at_all(self):
        plan = plan_swap([ent("sensor.cloud_only", "Signal")], [ent("switch.x")])
        assert plan.cloud_unmatched == ["sensor.cloud_only"]


class TestChoosingPairsByHand:
    """Where the matcher gives up, the person at the screen knows the answer.

    A plug's cloud switches are "Socket 1" and "Child lock"; tuya-local calls
    its own nothing and "Overcharge protection". Guessing would put a switch
    on the wrong relay, so the bridge asks instead of refusing outright.
    """

    def plan(self):
        from tuya_local_bridge.swap import plan_swap

        cloud = [
            ent("switch.plug_socket_1", "Socket 1"),
            ent("switch.plug_child_lock", "Child lock"),
        ]
        local = [
            ent("switch.hot_water"),
            ent("switch.hot_water_overcharge", "Overcharge protection"),
        ]
        p = plan_swap(cloud, local)
        assert p.pairs == [], "these must not pair automatically"
        return p

    def test_a_chosen_pairing_is_accepted(self):
        from tuya_local_bridge.swap import add_manual_pairs

        plan = self.plan()
        refused = add_manual_pairs(plan, {"switch.plug_socket_1": "switch.hot_water"})

        assert refused == {}
        assert len(plan.pairs) == 1
        assert plan.pairs[0].local_entity_id == "switch.hot_water"
        # And it is no longer on offer for anything else.
        assert "switch.plug_socket_1" not in plan.cloud_unmatched
        assert "switch.hot_water" not in plan.local_unmatched

    def test_only_same_domain_entities_are_offered(self):
        from tuya_local_bridge.swap import candidates_for, plan_swap

        p = plan_swap(
            [ent("switch.a", "Socket 1"), ent("sensor.b", "Signal")],
            [ent("switch.x"), ent("switch.y", "Other"), ent("light.z")],
        )
        offered = candidates_for(p, "switch.a")

        assert all(c.startswith("switch.") for c in offered)
        assert "light.z" not in offered

    def test_crossing_domains_is_refused(self):
        from tuya_local_bridge.swap import add_manual_pairs

        plan = self.plan()
        refused = add_manual_pairs(plan, {"switch.plug_socket_1": "light.something"})

        assert "switch.plug_socket_1" in refused
        assert plan.pairs == []

    def test_the_same_local_entity_cannot_be_used_twice(self):
        # A stale form submitted twice must not pair one entity into two slots.
        from tuya_local_bridge.swap import add_manual_pairs

        plan = self.plan()
        refused = add_manual_pairs(
            plan,
            {
                "switch.plug_socket_1": "switch.hot_water",
                "switch.plug_child_lock": "switch.hot_water",
            },
        )

        assert len(plan.pairs) == 1
        assert "switch.plug_child_lock" in refused

    def test_an_empty_choice_is_simply_skipped(self):
        from tuya_local_bridge.swap import add_manual_pairs

        plan = self.plan()
        assert add_manual_pairs(plan, {"switch.plug_socket_1": ""}) == {}
        assert plan.pairs == []
        assert "switch.plug_socket_1" in plan.cloud_unmatched

    def test_a_choice_for_something_already_paired_is_refused(self):
        from tuya_local_bridge.swap import add_manual_pairs, plan_swap

        p = plan_swap([ent("light.a")], [ent("light.b")])
        assert len(p.pairs) == 1

        refused = add_manual_pairs(p, {"light.a": "light.b"})
        assert "light.a" in refused
        assert len(p.pairs) == 1


class TestTheDeviceNameFollowsTheIds:
    """Entity ids carry automations; device names carry the humans.

    After a swap the local device holds every id that matters while the cloud
    device keeps the familiar name, so two identically named devices sit side
    by side and nothing says which is which.
    """

    CLOUD = {"id": "ha_cloud", "name": "ice ice machine", "name_by_user": None}
    LOCAL = {"id": "ha_local", "name": "ice ice machine", "name_by_user": None}

    class _Registry:
        def __init__(self, fail_on=None):
            self.calls = []
            self.fail_on = fail_on or set()

        def update_device(self, device_id, **changes):
            if device_id in self.fail_on:
                raise RuntimeError("boom")
            self.calls.append((device_id, changes))
            return {}

    def test_the_local_device_takes_the_name(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename

        store = ProvenanceStore(str(tmp_path / "p.json"))
        registry = self._Registry()

        result = apply_rename(registry, store, "abc", self.CLOUD, self.LOCAL)

        assert result == "ice ice machine"
        # Cloud first: two devices must not both hold the name, even briefly.
        assert registry.calls[0] == ("ha_cloud", {"name_by_user": "ice ice machine (cloud)"})
        assert registry.calls[1] == ("ha_local", {"name_by_user": "ice ice machine"})

    def test_a_name_the_user_chose_is_the_one_that_moves(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename

        cloud = {"id": "ha_cloud", "name": "hot water", "name_by_user": "Hot Water"}
        store = ProvenanceStore(str(tmp_path / "p.json"))
        registry = self._Registry()

        assert apply_rename(registry, store, "abc", cloud, self.LOCAL) == "Hot Water"

    def test_renaming_twice_does_nothing(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename

        store = ProvenanceStore(str(tmp_path / "p.json"))
        done = {"id": "ha_local", "name": "x", "name_by_user": "ice ice machine"}
        cloud_done = {"id": "ha_cloud", "name_by_user": "ice ice machine (cloud)"}

        assert apply_rename(self._Registry(), store, "abc", cloud_done, done) is None

    def test_a_half_done_rename_is_put_back(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename

        store = ProvenanceStore(str(tmp_path / "p.json"))
        registry = self._Registry(fail_on={"ha_local"})

        with pytest.raises(RuntimeError):
            apply_rename(registry, store, "abc", self.CLOUD, self.LOCAL)

        # The cloud name must not be left suffixed with nothing taking its place.
        assert registry.calls[-1] == ("ha_cloud", {"name_by_user": None})
        assert not store.devices.get("abc", None) or not store.get("abc").renames

    def test_the_rename_can_be_undone(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename, rollback_rename

        store = ProvenanceStore(str(tmp_path / "p.json"))
        apply_rename(self._Registry(), store, "abc", self.CLOUD, self.LOCAL)

        registry = self._Registry()
        assert rollback_rename(registry, store, "abc") == ""
        assert registry.calls[0][0] == "ha_local"
        assert registry.calls[1][0] == "ha_cloud"
        assert store.get("abc").active_rename is None

    def test_undoing_twice_does_nothing(self, tmp_path):
        from tuya_local_bridge.store import ProvenanceStore
        from tuya_local_bridge.swap import apply_rename, rollback_rename

        store = ProvenanceStore(str(tmp_path / "p.json"))
        apply_rename(self._Registry(), store, "abc", self.CLOUD, self.LOCAL)
        rollback_rename(self._Registry(), store, "abc")

        assert rollback_rename(self._Registry(), store, "abc") is None


# ── Devices whose two halves share no id at all ────────────────────────────

def _device(id_, name, **extra):
    return {"id": id_, "name": name, **extra}


def test_the_likeliest_predecessor_is_offered_first():
    """LEDVANCE bulbs reach Home Assistant through SmartThings.

    A SmartThings device carries a SmartThings uuid and no Tuya id anywhere,
    so nothing joins it to the tuya-local device holding the same bulb. The
    name is the only thing the two have in common, so rank on that and let
    the person confirm.
    """
    local = _device("local1", "gold light 1")
    candidates = suggest_predecessors(
        local,
        [
            local,
            _device("st1", "gold light 1"),
            _device("st2", "gold light 4"),
            _device("boiler", "Hot Water"),
        ],
        exclude={"local1"},
    )

    assert [d["id"] for d in candidates][:1] == ["st1"]
    assert "local1" not in [d["id"] for d in candidates]


def test_a_user_given_name_wins_over_the_integration_name():
    candidates = suggest_predecessors(
        _device("local1", "gold light 1"),
        [_device("st1", "TS0505B", name_by_user="gold light 1")],
    )
    assert [d["id"] for d in candidates] == ["st1"]


def test_every_local_device_is_kept_out_of_the_list():
    # Offering one converted device as another's predecessor would swap two
    # tuya-local entities with each other and disable one of them.
    candidates = suggest_predecessors(
        _device("local1", "gold light 1"),
        [_device("local2", "gold light 2"), _device("st1", "gold light 1")],
        exclude={"local1", "local2"},
    )
    assert [d["id"] for d in candidates] == ["st1"]


def test_the_offer_is_ordered_the_same_way_every_time():
    devices = [_device(f"d{i}", "same name") for i in range(5)]
    first = [d["id"] for d in suggest_predecessors(_device("x", "other"), devices)]
    second = [d["id"] for d in suggest_predecessors(_device("x", "other"), devices)]
    assert first == second
