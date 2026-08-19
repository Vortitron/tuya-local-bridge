import pytest

from tuya_local_bridge.ha_discovery import _unwrap, parse_device_registry, parse_flows

FLOW = {
    "context": {
        "source": "integration_discovery",
        "title_placeholders": {"name": "192.168.1.146"},
        "unique_id": "bf1000aa2000bb3000ccd1",
    },
    "flow_id": "01KYMPBMX2AAK02YN74X1P1AT9",
    "handler": "tuya_local",
    "step_id": "local",
}


def test_parses_a_real_tuya_local_discovery_flow():
    (dev,) = parse_flows([FLOW])
    assert dev.id == "bf1000aa2000bb3000ccd1"
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
        {"name": "Front Porch Local", "identifiers": [["tuya_local", "bf2000dd4000ee5000ffg2"]]},
        {"name": "Some Hue Lamp", "identifiers": [["hue", "00:17:88:01"]]},
    ]
    assert parse_device_registry(entries) == {"bf2000dd4000ee5000ffg2"}


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


def test_map_devices_pairs_cloud_and_local_representations():
    from tuya_local_bridge.ha_discovery import map_devices

    entries = [
        {"id": "ha_cloud", "identifiers": [["tuya", "bfa0ad92"]]},
        {"id": "ha_local", "identifiers": [["tuya_local", "bfa0ad92"]]},
        {"id": "unrelated", "identifiers": [["hue", "x"]]},
    ]
    assert map_devices(entries) == {
        "bfa0ad92": {"tuya": "ha_cloud", "tuya_local": "ha_local"}
    }


def test_map_devices_skips_entries_without_an_id():
    from tuya_local_bridge.ha_discovery import map_devices

    assert map_devices([{"identifiers": [["tuya", "abc"]]}]) == {}


class TestProxyRefusesRest:
    """Add-ons reach Home Assistant through a proxy with its own allow-list.

    `GET /api/config/config_entries/flow` is refused with 405 for a
    Supervisor token — a path that plainly exists, answering with a method
    error. The user saw "Internal Server Error" after scanning the QR code,
    with the real reason only in the add-on log.
    """

    @staticmethod
    def _response(status):
        class _R:
            status_code = status
            ok = status < 400
            text = "405: Method Not Allowed"

            @staticmethod
            def json():
                return {}
        return _R()

    def test_405_falls_back_to_websocket(self, monkeypatch):
        from tuya_local_bridge import ha_discovery, ha_ws

        monkeypatch.setattr(ha_discovery.requests, 'get',
                            lambda *a, **k: self._response(405))
        seen = {}

        def _command(base_url, token, payload, timeout=None):
            seen['payload'] = payload
            return []

        monkeypatch.setattr(ha_ws, 'command', _command)
        result = ha_discovery.from_home_assistant('http://supervisor/core', 'tok')
        assert result == []
        assert seen['payload']['type'] == 'config_entries/flow/progress'

    def test_other_errors_still_surface(self, monkeypatch):
        """A 500 is a real failure and must not be silently retried."""
        from tuya_local_bridge import ha_discovery

        monkeypatch.setattr(ha_discovery.requests, 'get',
                            lambda *a, **k: self._response(500))
        with pytest.raises(ha_discovery.HaDiscoveryError):
            ha_discovery.from_home_assistant('http://supervisor/core', 'tok')

    def test_a_websocket_failure_is_reported_usefully(self, monkeypatch):
        from tuya_local_bridge import ha_discovery, ha_ws

        monkeypatch.setattr(ha_discovery.requests, 'get',
                            lambda *a, **k: self._response(405))

        def _boom(*a, **k):
            raise ha_ws.HaWebSocketError('auth failed')

        monkeypatch.setattr(ha_ws, 'command', _boom)
        with pytest.raises(ha_discovery.HaDiscoveryError, match='auth failed'):
            ha_discovery.from_home_assistant('http://supervisor/core', 'tok')
