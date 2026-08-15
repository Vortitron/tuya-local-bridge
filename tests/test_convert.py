import pytest

from tuya_local_bridge.convert import (
    CONF_LOCAL_KEY,
    CONF_TYPE,
    convert,
    extract_options,
)
from tuya_local_bridge.models import CloudDevice, LanDevice, MatchedDevice


def device(key="k", ip="192.168.1.5", version="3.3"):
    return MatchedDevice(
        cloud=CloudDevice(id="abc", name="bulb", local_key=key),
        lan=LanDevice(id="abc", ip=ip, version=version),
    )


class FakeClient:
    """Returns queued steps and records what was submitted."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def continue_flow(self, flow_id, user_input):
        self.calls.append((flow_id, user_input))
        return self.steps.pop(0)


SELECT_TYPE = {
    "type": "form",
    "step_id": "select_type",
    "data_schema": [{"name": "type", "options": ["Smart bulb", "Generic switch"]}],
}
CREATED = {"type": "create_entry", "title": "bulb", "result": "01ENTRY"}


def test_first_call_submits_the_key_and_address():
    client = FakeClient(SELECT_TYPE)
    convert(client, device(), "flow1")

    (flow_id, submitted) = client.calls[0]
    assert flow_id == "flow1"
    assert submitted[CONF_LOCAL_KEY] == "k"
    assert submitted["host"] == "192.168.1.5"
    assert submitted["device_id"] == "abc"
    assert submitted["protocol_version"] == "3.3"


def test_stops_and_returns_type_options_rather_than_guessing():
    result = convert(FakeClient(SELECT_TYPE), device(), "flow1")

    assert result.status == "needs_type"
    assert result.type_options == ["Smart bulb", "Generic switch"]
    assert not result.ok


def test_supplying_a_type_completes_the_flow():
    client = FakeClient(SELECT_TYPE, CREATED)
    result = convert(client, device(), "flow1", device_type="Smart bulb")

    assert result.ok
    assert result.entry_id == "01ENTRY"
    assert client.calls[1][1] == {CONF_TYPE: "Smart bulb"}


def test_a_bad_local_key_is_reported_not_retried():
    rejected = {
        "type": "form",
        "step_id": "local",
        "errors": {"base": "connection_failed"},
    }
    client = FakeClient(rejected)
    result = convert(client, device(key="wrong"), "flow1", device_type="Smart bulb")

    assert result.status == "error"
    assert result.errors == {"base": "connection_failed"}
    # Must not have gone on to submit a type against a failed step.
    assert len(client.calls) == 1


def test_abort_is_surfaced_with_its_reason():
    client = FakeClient({"type": "abort", "reason": "already_configured"})
    result = convert(client, device(), "flow1")

    assert result.status == "error"
    assert result.message == "already_configured"


def test_missing_protocol_version_falls_back_to_auto():
    client = FakeClient(SELECT_TYPE)
    convert(client, device(version=""), "flow1")
    assert client.calls[0][1]["protocol_version"] == "auto"


def test_explicit_protocol_version_overrides_discovery():
    client = FakeClient(SELECT_TYPE)
    convert(client, device(version="3.3"), "flow1", protocol_version="3.5")
    assert client.calls[0][1]["protocol_version"] == "3.5"


def test_device_creating_an_entry_without_a_type_step():
    result = convert(FakeClient(CREATED), device(), "flow1")
    assert result.ok


def test_unknown_step_is_an_error_not_a_crash():
    client = FakeClient({"type": "form", "step_id": "something_new"})
    result = convert(client, device(), "flow1")
    assert result.status == "error"
    assert "something_new" in result.message


def test_garbage_response_is_an_error_not_a_crash():
    assert convert(FakeClient("nope"), device(), "flow1").status == "error"


@pytest.mark.parametrize(
    "schema",
    [
        [{"name": "type", "options": ["a", "b"]}],
        [{"name": "type", "options": [{"value": "a", "label": "A"}, {"value": "b"}]}],
        [{"name": "type", "selector": {"select": {"options": ["a", "b"]}}}],
        [{"name": "type", "options": [["a", "A"], ["b", "B"]]}],
    ],
)
def test_option_shapes_home_assistant_has_used(schema):
    # HA serialises voluptuous loosely and this shape has changed over releases.
    assert extract_options({"data_schema": schema}, CONF_TYPE) == ["a", "b"]


def test_options_for_an_absent_field():
    assert extract_options({"data_schema": [{"name": "other", "options": ["x"]}]}, CONF_TYPE) == []
    assert extract_options({}, CONF_TYPE) == []
