from tuya_local_bridge.match import reconcile
from tuya_local_bridge.models import CloudDevice, LanDevice


def cloud(id_, name="dev", key="k", sub=False):
    return CloudDevice(id=id_, name=name, local_key=key, sub=sub)


def lan(id_, ip, version="3.3"):
    return LanDevice(id=id_, ip=ip, version=version)


def test_matches_on_device_id_not_address():
    # The cloud reports a WAN address; the join must ignore it entirely.
    c = CloudDevice(id="abc", name="bulb", local_key="k", wan_ip="213.65.215.158")
    result = reconcile([c], [lan("abc", "192.168.1.146")])

    assert result.counts == {"matched": 1, "cloud_only": 0, "lan_only": 0}
    assert result.matched[0].config == {
        "device_id": "abc",
        "host": "192.168.1.146",
        "local_key": "k",
        "protocol_version": "3.3",
    }


def test_cloud_device_absent_from_lan_is_cloud_only():
    result = reconcile([cloud("abc")], [])
    assert [c.id for c in result.cloud_only] == ["abc"]
    assert result.matched == []


def test_lan_device_absent_from_cloud_is_lan_only():
    # The silent-failure case: re-paired device, so any cached key is stale.
    result = reconcile([], [lan("ghost", "192.168.1.99")])
    assert [d.id for d in result.lan_only] == ["ghost"]


def test_sub_devices_are_never_matched_or_listed():
    # A Zigbee bulb behind a hub has no local key and never will.
    zigbee = cloud("zb", sub=True, key="")
    result = reconcile([zigbee], [lan("zb", "192.168.1.5")])

    assert result.matched == []
    assert result.cloud_only == []
    # It was consumed, so it must not resurface as an unknown LAN device.
    assert result.lan_only == []


def test_unconvertible_can_be_shown_on_request():
    result = reconcile([cloud("zb", sub=True, key="")], [], include_unconvertible=True)
    assert [c.id for c in result.cloud_only] == ["zb"]


def test_device_without_key_is_not_matched():
    result = reconcile([cloud("abc", key="")], [lan("abc", "192.168.1.5")])
    assert result.matched == []
    assert result.lan_only == []


def test_missing_protocol_version_falls_back_to_auto():
    result = reconcile([cloud("abc")], [lan("abc", "192.168.1.5", version="")])
    assert result.matched[0].config["protocol_version"] == "auto"


def test_lan_records_without_an_id_are_ignored():
    result = reconcile([cloud("abc")], [lan("", "192.168.1.5")])
    assert result.lan_only == []
    assert [c.id for c in result.cloud_only] == ["abc"]


def test_results_are_deterministically_ordered():
    result = reconcile(
        [cloud("b", name="Zeta"), cloud("a", name="alpha")],
        [lan("b", "192.168.1.20"), lan("a", "192.168.1.3")],
    )
    assert [m.cloud.name for m in result.matched] == ["alpha", "Zeta"]
