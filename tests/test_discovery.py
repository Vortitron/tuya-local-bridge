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
