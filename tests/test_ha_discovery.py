from tuya_local_bridge.ha_discovery import _unwrap, parse_device_registry, parse_flows

FLOW = {
    "context": {
        "source": "integration_discovery",
        "title_placeholders": {"name": "192.168.1.146"},
        "unique_id": "bf9986ce08f32502cbglz1",
    },
    "flow_id": "01KYMPBMX2AAK02YN74X1P1AT9",
    "handler": "tuya_local",
    "step_id": "local",
}


def test_parses_a_real_tuya_local_discovery_flow():
    (dev,) = parse_flows([FLOW])
    assert dev.id == "bf9986ce08f32502cbglz1"
    assert dev.ip == "192.168.1.146"
    # HA's flow does not carry the protocol version; tuya-local probes for it.
    assert dev.version == ""


def test_ignores_other_integrations_by_default():
    esphome = {**FLOW, "handler": "esphome"}
    assert parse_flows([esphome]) == []
    assert len(parse_flows([esphome], handler=None)) == 1


def test_skips_flows_without_a_unique_id():
    flow = {"handler": "tuya_local", "context": {"title_placeholders": {"name": "192.168.1.5"}}}
    assert parse_flows([flow]) == []


def test_skips_flows_whose_placeholder_is_a_name_not_an_address():
    # Some integrations put a friendly name here; we must not treat it as a host.
    flow = {
        "handler": "tuya_local",
        "context": {"unique_id": "abc", "title_placeholders": {"name": "Kitchen Lamp"}},
    }
    assert parse_flows([flow]) == []


def test_rejects_out_of_range_octets():
    flow = {
        "handler": "tuya_local",
        "context": {"unique_id": "abc", "title_placeholders": {"name": "192.168.1.999"}},
    }
    assert parse_flows([flow]) == []


def test_falls_back_to_a_host_key_in_context():
    flow = {"handler": "tuya_local", "context": {"unique_id": "abc", "host": "192.168.1.7"}}
    (dev,) = parse_flows([flow])
    assert dev.ip == "192.168.1.7"


def test_tolerates_junk_entries():
    assert parse_flows([None, "nonsense", 42, FLOW]) == parse_flows([FLOW])


def test_unwraps_the_brokered_envelope():
    assert _unwrap({"result": [FLOW]}) == [FLOW]
    assert _unwrap({"result": {"result": [FLOW]}}) == [FLOW]
    assert _unwrap([FLOW]) == [FLOW]
    assert _unwrap({"error": "nope"}) == []


def test_unwrap_gives_up_rather_than_looping_forever():
    payload = {"result": {"result": {"result": {"result": [FLOW]}}}}
    assert _unwrap(payload) == []


def test_parses_tuya_local_devices_out_of_the_registry():
    entries = [
        {"name": "Front Porch Local", "identifiers": [["tuya_local", "bfa0ad92d1a9e36c1cqmlb"]]},
        {"name": "Some Hue Lamp", "identifiers": [["hue", "00:17:88:01"]]},
    ]
    assert parse_device_registry(entries) == {"bfa0ad92d1a9e36c1cqmlb"}


def test_registry_entry_with_several_identifiers():
    entries = [{"identifiers": [["mqtt", "x"], ["tuya_local", "abc"]]}]
    assert parse_device_registry(entries) == {"abc"}


def test_registry_ignores_malformed_identifiers():
    entries = [
        {"identifiers": [["tuya_local"]]},
        {"identifiers": [["tuya_local", ""]]},
        {"identifiers": None},
        "junk",
    ]
    assert parse_device_registry(entries) == set()


def test_registry_domain_is_selectable():
    entries = [{"identifiers": [["esphome", "node1"]]}]
    assert parse_device_registry(entries, domain="esphome") == {"node1"}


def test_websocket_url_rewriting():
    from tuya_local_bridge.ha_ws import websocket_url

    assert websocket_url("http://supervisor/core") == "ws://supervisor/core/api/websocket"
    assert websocket_url("https://ha.example.com/") == "wss://ha.example.com/api/websocket"
    assert websocket_url("homeassistant:8123") == "ws://homeassistant:8123/api/websocket"
