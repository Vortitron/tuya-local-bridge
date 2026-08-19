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


class TestFlowValidationErrors:
    """Home Assistant answers a rejected config-flow step with HTTP 400.

    The body carries the reasons, so raising on the status throws away the
    only useful part of the reply. That is why the setup_mode handling did
    not fire: the exception was raised before anything could look at it, and
    the user saw the raw JSON as an error message twice over.
    """

    class _Response:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self.ok = status < 400
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json")
            return self._payload

    REJECTION = {
        "errors": {
            "base": ["extra keys not allowed @ data['device_id']"],
            "setup_mode": "required key not provided",
        }
    }

    def test_a_400_is_returned_as_a_step(self):
        from tuya_local_bridge import convert as conv

        step = conv._flow_response(self._Response(400, self.REJECTION), "Home Assistant")
        assert step["type"] == "form", 'callers key off the step type'
        assert step["errors"]["setup_mode"]

    def test_the_setup_mode_handling_now_sees_it(self):
        """The two fixes only work together."""
        from tuya_local_bridge import convert as conv

        step = conv._flow_response(self._Response(400, self.REJECTION), "Home Assistant")
        assert conv._wants_setup_mode(step) is True

    def test_other_statuses_still_raise(self):
        from tuya_local_bridge import convert as conv

        for status in (401, 404, 500):
            with pytest.raises(conv.FlowError):
                conv._flow_response(self._Response(status, text="nope"), "Home Assistant")

    def test_a_400_without_a_usable_body_still_raises(self):
        """An empty or non-JSON 400 is a real failure, not a form."""
        from tuya_local_bridge import convert as conv

        with pytest.raises(conv.FlowError):
            conv._flow_response(self._Response(400, None, text="bad request"), "Home Assistant")


def test_a_real_rejection_drives_the_whole_conversion(monkeypatch):
    """End to end over HTTP, with the exact payload Home Assistant returned.

    The previous attempt tested the setup_mode handling against a step dict
    that the client could never actually produce, so it passed while the
    real path still failed. This drives convert() through the HTTP client.
    """
    from tuya_local_bridge import convert as conv

    sent = []
    replies = [
        # 1: device fields, rejected because the mode was not chosen
        (400, {"errors": {"base": ["extra keys not allowed @ data['device_id']"],
                          "setup_mode": "required key not provided"},
               "data_schema": [{"name": "setup_mode",
                                "options": ["cloud", "manual"]}]}),
        # 2: mode accepted, the device form follows
        (200, {"type": "form", "step_id": "local"}),
        # 3: device fields accepted
        (200, {"type": "create_entry", "result": "entry-9", "title": "Hot water"}),
    ]

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self.ok = status < 400
            self._p = payload
            self.text = str(payload)

        def json(self):
            return self._p

    def _post(url, headers=None, data=None, timeout=None):
        import json as _json
        sent.append(_json.loads(data))
        return _Resp(*replies[len(sent) - 1])

    monkeypatch.setattr(conv.requests, 'post', _post)
    client = conv.DirectFlowClient('http://ha.local', 'tok')

    import tests.test_convert as mod
    device = None
    for name in ('make_device', 'device', '_device'):
        if hasattr(mod, name):
            device = getattr(mod, name)()
            break
    if device is None:
        pytest.skip('no device factory in this module')

    result = conv.convert(client, device, flow_id='f1')

    assert len(sent) == 3, f'expected reject, mode, retry — got {len(sent)} calls'
    assert 'device_id' in sent[0]
    assert sent[1] == {'setup_mode': 'manual'}
    assert 'device_id' in sent[2]
    assert result.status == 'created'


def test_setup_mode_options_are_read_from_a_real_schema():
    """The shape a live tuya-local flow actually returns.

    Home Assistant nests select options under a selector rather than a flat
    "options" key. Reading only the top level found nothing and fell back to
    a hardcoded default that happened to match — which is luck, not a fix.
    """
    from tuya_local_bridge import convert as conv

    step = {
        "type": "form",
        "errors": {"setup_mode": "required key not provided"},
        "data_schema": [
            {
                "name": "setup_mode",
                "required": True,
                "selector": {
                    "select": {
                        "options": ["cloud", "manual", "cloud_fresh_login"],
                        "mode": "list",
                    }
                },
            }
        ],
    }
    assert conv._pick_setup_mode(step) == "manual", \
        'must choose manual entry, not a cloud login we do not need'


class TestTheFlowIsAskedNotGuessed:
    """Drive the flow from what it declares, not from how it complains.

    tuya-local put a mode-selection step in front of the device form. Posting
    the device fields to it is rejected wholesale -- "extra keys not allowed"
    for every field. Sometimes the reply also names the missing setup_mode,
    and sometimes it says only "base":

        base: ["extra keys not allowed @ data['device_id']", ...]

    A rejection carries no schema, so there is nothing in that reply to work
    from. Reading the flow answers the question directly.
    """

    MODE_STEP = {
        "type": "form",
        "step_id": "user",
        "data_schema": [
            {
                "name": "setup_mode",
                "selector": {
                    "select": {"options": ["cloud", "manual", "cloud_fresh_login"]}
                },
            }
        ],
    }

    EXTRA_KEYS = {
        "type": "form",
        "errors": {
            "base": [
                "extra keys not allowed @ data['device_id']",
                "extra keys not allowed @ data['host']",
                "extra keys not allowed @ data['local_key']",
            ]
        },
    }

    class Client:
        """Answers like tuya-local: nothing but setup_mode until it is given."""

        def __init__(self, step, replies):
            self._step = step
            self._replies = list(replies)
            self.posted = []

        def current_step(self, flow_id):
            return self._step

        def continue_flow(self, flow_id, user_input):
            self.posted.append(user_input)
            return self._replies.pop(0)

    def test_the_mode_step_is_answered_before_the_device_fields(self):
        client = self.Client(
            self.MODE_STEP,
            [
                {"type": "form", "step_id": "user"},
                {"type": "create_entry", "result": "e1", "title": "floodlight"},
            ],
        )
        result = convert(client, device(), "flow1")

        assert result.ok, result.message
        assert client.posted[0] == {"setup_mode": "manual"}, (
            "the mode must be answered first, from the options the flow offers"
        )
        assert "device_id" in client.posted[1]

    def test_a_bare_base_rejection_still_gets_a_useful_message(self):
        """The floodlight case: no setup_mode key, no schema, just "base"."""
        client = self.Client(
            # The flow has moved on and now wants something we do not send.
            {
                "type": "form",
                "step_id": "user",
                "data_schema": [{"name": "setup_mode"}, {"name": "something_new"}],
            },
            [self.EXTRA_KEYS, self.EXTRA_KEYS],
        )
        result = convert(client, device(), "flow1")

        assert result.status == "error"
        assert "extra keys not allowed" not in result.message, (
            "voluptuous internals must not reach the owner"
        )
        assert "setup_mode" in result.message and "something_new" in result.message, (
            "say what the form is actually asking for"
        )
        assert "host" in result.message, "and what we sent"

    def test_a_client_that_cannot_read_flows_still_converts(self):
        """current_step is optional; the old inference must still work."""

        class Old:
            def __init__(self):
                self.posted = []

            def continue_flow(self, flow_id, user_input):
                self.posted.append(user_input)
                return {"type": "create_entry", "result": "e1", "title": "x"}

        client = Old()
        assert convert(client, device(), "flow1").ok
        assert len(client.posted) == 1

    def test_a_failed_read_does_not_break_the_conversion(self):
        class Broken:
            def current_step(self, flow_id):
                raise RuntimeError("no route to Home Assistant")

            def continue_flow(self, flow_id, user_input):
                return {"type": "create_entry", "result": "e1", "title": "x"}

        assert convert(Broken(), device(), "flow1").ok
