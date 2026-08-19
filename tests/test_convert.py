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


class TestSetupModeStep:
    """Newer tuya-local asks which setup mode you want before the device form.

    On that version the device fields are rejected wholesale — "extra keys
    not allowed" for every one, plus "setup_mode: required key not provided"
    — which reads as a broken integration rather than a flow with one more
    step than it had before. This is the exact payload a real conversion
    returned.
    """

    REJECTION = {
        "type": "form",
        "step_id": "user",
        "errors": {
            "base": [
                "extra keys not allowed @ data['device_id']",
                "extra keys not allowed @ data['host']",
                "extra keys not allowed @ data['local_key']",
            ],
            "setup_mode": "required key not provided",
        },
        "data_schema": [
            {"name": "setup_mode", "options": ["cloud", "manual", "smart_life"]},
        ],
    }

    class _Client:
        def __init__(self, replies):
            self.replies = list(replies)
            self.sent = []

        def continue_flow(self, flow_id, user_input):
            self.sent.append(user_input)
            return self.replies.pop(0)

    def _device(self):
        import tests.test_convert as mod
        for name in ('make_device', 'device', '_device'):
            if hasattr(mod, name):
                return getattr(mod, name)()
        pytest.skip('no device factory in this module')

    def test_the_mode_is_answered_then_the_device_fields_resent(self, monkeypatch):
        from tuya_local_bridge import convert as conv

        device = self._device()
        client = self._Client([
            self.REJECTION,
            {"type": "form", "step_id": "local"},
            {"type": "create_entry", "result": "entry-1", "title": "Hot water"},
        ])
        result = conv.convert(client, device, flow_id="f1")

        assert [s.get('setup_mode') for s in client.sent if 'setup_mode' in s] == ['manual'], \
            'should answer the mode with the manual option, not cloud'
        assert len(client.sent) == 3, 'device fields must be sent again after the mode'
        assert client.sent[2]['device_id'] == client.sent[0]['device_id']
        assert result.status == 'created'

    def test_the_old_flow_is_unchanged(self):
        """A version without the mode step must not gain an extra round trip."""
        from tuya_local_bridge import convert as conv

        client = self._Client([
            {"type": "create_entry", "result": "entry-1", "title": "Hot water"},
        ])
        result = conv.convert(client, self._device(), flow_id="f1")
        assert len(client.sent) == 1
        assert result.status == 'created'
