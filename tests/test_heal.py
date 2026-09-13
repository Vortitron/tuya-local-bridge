import pytest

from tuya_local_bridge.heal import (
    DRIFT_ADDRESS,
    DRIFT_KEY,
    DRIFT_VERSION,
    Drift,
    HealError,
    detect_drift,
    entry_ids_for_devices,
    repair,
)
from tuya_local_bridge.models import CloudDevice, LanDevice
from tuya_local_bridge.store import ProvenanceStore


def store_with(tmp_path, *, ip="192.168.1.5", key="k1", version="3.3"):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([CloudDevice(id="abc", name="bulb", local_key=key)], now=100.0)
    store.record_lan([LanDevice(id="abc", ip=ip, version=version)], now=100.0)
    return store


ENTRIES = {"abc": "entry1"}


def test_no_drift_when_nothing_moved(tmp_path):
    store = store_with(tmp_path)
    drifts = detect_drift(
        [CloudDevice(id="abc", name="bulb", local_key="k1")],
        [LanDevice(id="abc", ip="192.168.1.5", version="3.3")],
        store,
        ENTRIES,
    )
    assert drifts == []


def test_dhcp_move_is_detected(tmp_path):
    store = store_with(tmp_path)
    (drift,) = detect_drift(
        [CloudDevice(id="abc", name="bulb", local_key="k1")],
        [LanDevice(id="abc", ip="192.168.1.99", version="3.3")],
        store,
        ENTRIES,
    )
    assert drift.kinds == [DRIFT_ADDRESS]
    assert "192.168.1.5 -> 192.168.1.99" in drift.describe()


def test_key_rotation_is_detected(tmp_path):
    store = store_with(tmp_path)
    (drift,) = detect_drift(
        [CloudDevice(id="abc", name="bulb", local_key="k2")],
        [LanDevice(id="abc", ip="192.168.1.5", version="3.3")],
        store,
        ENTRIES,
    )
    assert drift.kinds == [DRIFT_KEY]
    assert "rotated" in drift.describe()


def test_both_at_once(tmp_path):
    store = store_with(tmp_path)
    (drift,) = detect_drift(
        [CloudDevice(id="abc", name="bulb", local_key="k2")],
        [LanDevice(id="abc", ip="192.168.1.99", version="3.5")],
        store,
        ENTRIES,
    )
    assert drift.kinds == [DRIFT_ADDRESS, DRIFT_KEY, DRIFT_VERSION]


def test_devices_that_were_never_converted_are_skipped(tmp_path):
    # Nothing is configured, so nothing can be stale.
    store = store_with(tmp_path)
    assert detect_drift(
        [CloudDevice(id="abc", name="bulb", local_key="k2")],
        [LanDevice(id="abc", ip="192.168.1.99")],
        store,
        {},
    ) == []


def test_a_device_that_is_simply_offline_is_not_drift(tmp_path):
    # Absent from the LAN tells us nothing about its address; do not guess.
    store = store_with(tmp_path)
    assert detect_drift([CloudDevice(id="abc", name="bulb", local_key="k1")], [], store, ENTRIES) == []


def test_entry_ids_come_from_the_device_registry():
    registry = [
        {"identifiers": [["tuya_local", "abc"]], "config_entries": ["entry1"]},
        {"identifiers": [["tuya", "abc"]], "config_entries": ["cloud_entry"]},
        {"identifiers": [["tuya_local", "def"]], "config_entries": []},
    ]
    assert entry_ids_for_devices(registry) == {"abc": "entry1"}


# ── repair ─────────────────────────────────────────────────────────────────


class FakeFlow:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    start_step = {"flow_id": "f1", "type": "form", "step_id": "init"}

    def start_options_flow(self, entry_id):
        self.calls.append(("start", entry_id))
        return self.start_step

    def continue_options_flow(self, flow_id, user_input):
        self.calls.append(("continue", user_input))
        return self.steps.pop(0)


def drift_fixture():
    return Drift(
        device_id="abc",
        entry_id="entry1",
        kinds=[DRIFT_ADDRESS],
        known_ip="192.168.1.5",
        current_ip="192.168.1.99",
        current_key="k1",
        current_version="3.3",
    )


def test_repair_fills_in_what_the_form_asks_for():
    """The options form is not the config form.

    tuya-local's options step wants a key, a host and a protocol, and rejects
    a device id outright — "not a valid option at 'device_id'" — because an
    entry that already exists knows which device it is. Assuming the config
    flow's shape here cost a release.
    """
    flow = FakeFlow({"type": "create_entry"})
    flow.start_step = {
        "flow_id": "f1",
        "type": "form",
        "step_id": "user",
        "data_schema": [
            {"name": "local_key", "required": True, "type": "string"},
            {"name": "host", "required": True, "type": "string"},
            {"name": "protocol_version", "options": ["auto", "3.3", "3.4"]},
            {"name": "poll_only", "default": False, "type": "boolean"},
        ],
    }

    assert repair(flow, drift_fixture()) == "repaired"

    _, submitted = flow.calls[1]
    assert "device_id" not in submitted, "the options form refuses it"
    assert submitted["host"] == "192.168.1.99"
    assert submitted["local_key"] == "k1"
    assert submitted["protocol_version"] == "3.3"
    assert submitted["poll_only"] is False


def test_a_protocol_the_form_will_not_take_becomes_auto():
    # Better a working entry on auto than a 400 for an unlisted version.
    flow = FakeFlow({"type": "create_entry"})
    flow.start_step = {
        "flow_id": "f1",
        "type": "form",
        "data_schema": [
            {"name": "host", "type": "string"},
            {"name": "protocol_version", "options": ["auto", "3.3"]},
        ],
    }
    drift = drift_fixture()
    drift.current_version = "3.5"

    repair(flow, drift)

    assert flow.calls[1][1]["protocol_version"] == "auto"


def test_a_form_with_no_readable_schema_falls_back_to_what_it_wants():
    flow = FakeFlow({"type": "create_entry"})
    repair(flow, drift_fixture())

    _, submitted = flow.calls[1]
    assert set(submitted) == {"local_key", "host", "protocol_version"}
    assert "device_id" not in submitted


def test_repair_falls_back_to_known_values():
    drift = Drift(device_id="abc", entry_id="entry1", known_ip="192.168.1.5", known_key="k1")
    flow = FakeFlow({"type": "create_entry"})
    repair(flow, drift)

    _, submitted = flow.calls[1]
    assert submitted["host"] == "192.168.1.5"
    assert submitted["protocol_version"] == "auto"


def test_repair_without_an_entry_is_refused():
    with pytest.raises(HealError):
        repair(FakeFlow(), Drift(device_id="abc"))


def test_form_errors_are_raised():
    flow = FakeFlow({"type": "form", "errors": {"base": "connection_failed"}})
    with pytest.raises(HealError) as excinfo:
        repair(flow, drift_fixture())
    assert "connection_failed" in str(excinfo.value)


def test_abort_is_raised_with_its_reason():
    flow = FakeFlow({"type": "abort", "reason": "cannot_connect"})
    with pytest.raises(HealError) as excinfo:
        repair(flow, drift_fixture())
    assert "cannot_connect" in str(excinfo.value)


def test_a_repaired_entry_is_reloaded():
    """Saving the options is not enough to bring the entry back.

    An entry in setup_retry keeps retrying on its own schedule with the values
    it already failed on, so the repair looked like it had done nothing until
    the integration was reloaded by hand.
    """
    class Reloading(FakeFlow):
        reloaded = None

        def reload_entry(self, entry_id):
            type(self).reloaded = entry_id

    flow = Reloading({"type": "create_entry"})
    assert repair(flow, drift_fixture()) == "repaired"
    assert Reloading.reloaded == "entry1"


def test_a_reload_that_fails_does_not_undo_the_repair():
    # The values are saved either way; a reload that will not happen is worth
    # a log line, not an exception that reports the repair as failed.
    class Stubborn(FakeFlow):
        def reload_entry(self, entry_id):
            raise RuntimeError("boom")

    assert repair(Stubborn({"type": "create_entry"}), drift_fixture()) == "repaired"
