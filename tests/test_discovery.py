from tuya_local_bridge.discovery import _normalise


def test_normalises_tinytuya_scan_output():
    found = {
        "192.168.1.146": {
            "ip": "192.168.1.146",
            "gwId": "bf1000aa2000bb3000ccd1",
            "version": 3.5,
            "productKey": "xibcsv6dp3ajzth9",
        }
    }
    (dev,) = _normalise(found)

    assert dev.id == "bf1000aa2000bb3000ccd1"
    assert dev.ip == "192.168.1.146"
    assert dev.version == "3.5"  # normalised from a float


def test_falls_back_to_the_dict_key_for_the_address():
    (dev,) = _normalise({"192.168.1.5": {"gwId": "abc", "version": "3.3"}})
    assert dev.ip == "192.168.1.5"


def test_skips_records_without_a_device_id():
    assert _normalise({"192.168.1.5": {"ip": "192.168.1.5"}}) == []


def test_skips_non_dict_values():
    assert _normalise({"error": "no devices found"}) == []


def test_handles_an_empty_scan():
    assert _normalise({}) == []
    assert _normalise(None) == []


def test_sorts_numerically_by_address_not_lexically():
    found = {
        "192.168.1.100": {"ip": "192.168.1.100", "gwId": "c"},
        "192.168.1.20": {"ip": "192.168.1.20", "gwId": "b"},
        "192.168.1.3": {"ip": "192.168.1.3", "gwId": "a"},
    }
    assert [d.ip for d in _normalise(found)] == [
        "192.168.1.3",
        "192.168.1.20",
        "192.168.1.100",
    ]


def test_a_deep_scan_never_waits_for_an_answer(monkeypatch):
    """A forced subnet scan must not prompt.

    tinytuya.deviceScan does not pass assume_yes, so the scanner calls
    input() to confirm each auto-detected network. At a terminal that is
    helpful; inside the add-on stdin is closed, so the scan blocks forever
    or dies on EOFError with nobody there to answer.
    """
    import sys
    import types

    seen = {}

    def fake_devices(**kwargs):
        seen.update(kwargs)
        return {}

    fake_scanner = types.ModuleType("tinytuya.scanner")
    fake_scanner.devices = fake_devices
    fake_tinytuya = types.ModuleType("tinytuya")
    fake_tinytuya.scanner = fake_scanner

    def boom(*a, **k):
        raise AssertionError("deviceScan cannot suppress the prompt")

    fake_tinytuya.deviceScan = boom
    monkeypatch.setitem(sys.modules, "tinytuya", fake_tinytuya)
    monkeypatch.setitem(sys.modules, "tinytuya.scanner", fake_scanner)

    from tuya_local_bridge import discovery

    discovery.scan(3, force_subnet_scan=True)

    assert seen.get("assume_yes") is True
    assert seen.get("forcescan") is True
