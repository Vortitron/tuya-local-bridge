import pytest

from tuya_local_bridge.match import reconcile
from tuya_local_bridge.models import CloudDevice, LanDevice
from tuya_local_bridge.web import _esc, _qr_svg, _render_status

pytest.importorskip("flask")


def cloud(id_="abc", name="bulb", key="k"):
    return CloudDevice(id=id_, name=name, local_key=key, online=True)


def lan(id_="abc", ip="192.168.1.5"):
    return LanDevice(id=id_, ip=ip)


def test_matched_devices_are_selectable():
    result = reconcile([cloud()], [lan()])
    html = _render_status(result, {"abc": "flow1"}, [])

    assert 'name="device" value="abc"' in html
    assert "disabled" not in html


def test_a_device_without_a_flow_cannot_be_selected():
    # No discovery flow means nothing to continue, so the box must be dead
    # rather than silently failing on submit.
    result = reconcile([cloud()], [lan()])
    html = _render_status(result, {}, [])

    assert 'name="device" value="abc"' in html
    assert "disabled" in html


def test_key_rotation_is_called_out_prominently():
    result = reconcile([cloud()], [lan()])
    html = _render_status(result, {"abc": "flow1"}, ["abc"])
    assert "Local key changed" in html


def test_no_rotation_notice_when_nothing_rotated():
    html = _render_status(reconcile([cloud()], [lan()]), {"abc": "flow1"}, [])
    assert "Local key changed" not in html


def test_unexplained_lan_devices_are_explained_to_the_user():
    result = reconcile([], [lan(id_="ghost", ip="192.168.1.99")])
    html = _render_status(result, {}, [])
    assert "192.168.1.99" in html
    assert "another brand" in html


def test_device_names_are_escaped():
    result = reconcile([cloud(name='<script>alert("x")</script>')], [lan()])
    html = _render_status(result, {"abc": "flow1"}, [])

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_esc_covers_quotes_and_ampersands():
    assert _esc('a&b"c<d>') == "a&amp;b&quot;c&lt;d&gt;"


def test_qr_renders_as_inline_svg_not_a_remote_asset():
    svg = _qr_svg("tuyaSmart--qrLogin?token=abc")
    assert svg.startswith("<svg")
    assert "http://www.w3.org/2000/svg" in svg
    # Nothing may be fetched from outside — ingress CSP would block it.
    assert "src=" not in svg and "data:" not in svg
