"""A device that has changed address must not fail with "base: connection".

Addresses reach us from two places and both go stale:

  * Home Assistant's discovery remembers every Tuya device it has ever heard
    and never expires the address it recorded.
  * A plain UDP scan only refreshes devices still broadcasting, and over a
    bridged tunnel some are not heard at all.

Handing tuya-local a dead address gets ``base: connection`` back, which is
accurate and tells the owner nothing they can act on.  On a real house four of
eleven discovered addresses had already moved.
"""
from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")

from tuya_local_bridge import web  # noqa: E402
from tuya_local_bridge.models import CloudDevice, LanDevice  # noqa: E402


STALE, CURRENT = "192.168.1.110", "192.168.1.78"


class _Session:
    def devices(self):
        return [
            CloudDevice(id="hw", name="hot water", local_key="k", online=True)
        ]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """An app whose only device has silently moved to a new address."""
    state = {"deep_scans": 0, "submitted": []}

    monkeypatch.setattr(
        web.cloud_mod.TuyaCloudSession, "load", staticmethod(lambda p: _Session())
    )
    # HA's discovery holds the address it recorded when it first saw the
    # device, and carries the flow id that conversion needs.
    monkeypatch.setattr(
        web.ha_discovery,
        "from_home_assistant",
        lambda *a, **k: [
            LanDevice(id="hw", ip=STALE, raw={"flow_id": "flow1"})
        ],
    )
    monkeypatch.setattr(
        web.ha_discovery, "converted_from_home_assistant", lambda *a, **k: set()
    )

    def fake_scan(seconds, *, force_subnet_scan=False):
        # The device is not broadcasting, so only a subnet probe finds it.
        if not force_subnet_scan:
            return []
        state["deep_scans"] += 1
        return [LanDevice(id="hw", ip=CURRENT, version="3.3")]

    monkeypatch.setattr(web.discovery_mod, "scan", fake_scan)
    monkeypatch.setattr(web, "reachable", lambda ip, **k: ip == CURRENT)

    def fake_convert(client, matched, flow_id, **kwargs):
        state["submitted"].append(matched.lan.ip)
        return _created(matched)

    def _created(matched):
        from tuya_local_bridge.convert import ConversionResult

        return ConversionResult(
            device_id=matched.id, status="created", entry_id="e1", title="hot water"
        )

    monkeypatch.setattr(web, "convert", fake_convert)

    app = web.create_app(
        state_dir=str(tmp_path),
        ha_url="http://ha.local:8123",
        ha_token="t",
        scan_seconds=1,
    )
    app.config.update(TESTING=True)
    return app.test_client(), state


def test_a_moved_device_is_found_and_converted(rig):
    """The conversion must use the address the device answers on."""
    client, state = rig
    response = client.post("/convert", data={"device": "hw"})

    assert response.status_code == 200
    assert state["deep_scans"] == 1, "an unreachable device must trigger a deep scan"
    assert state["submitted"] == [CURRENT], (
        f"tuya-local was handed {state['submitted']}, not the address the "
        "device actually answers on"
    )


def test_a_reachable_device_does_not_pay_for_a_deep_scan(tmp_path, monkeypatch, rig):
    """Deep scans probe the whole subnet, so they must stay exceptional."""
    client, state = rig
    monkeypatch.setattr(web, "reachable", lambda ip, **k: True)

    client.post("/convert", data={"device": "hw"})
    assert state["deep_scans"] == 0


def test_a_device_that_is_really_gone_says_so_usefully(tmp_path, monkeypatch, rig):
    """When even a deep scan cannot find it, say what was tried."""
    client, state = rig
    monkeypatch.setattr(web, "reachable", lambda ip, **k: False)

    body = client.post("/convert", data={"device": "hw"}).get_data(as_text=True)

    assert "base: connection" not in body
    assert "6668" in body, "name the port that was tried"
    assert "switched off" in body or "moved" in body, (
        "tell the owner what to check"
    )


def test_the_button_asks_before_it_writes(rig):
    """"Convert selected" must not be the point of no return."""
    client, state = rig
    response = client.post("/convert/confirm", data={"device": "hw"})
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert state["submitted"] == [], "nothing may be written before confirming"
    assert "hot water" in body, "name what is about to change"
    assert "Device type" in body, "warn that a choice is still coming"
    assert 'action="/convert"' in body, "and offer the way through"


def test_confirming_nothing_goes_back(rig):
    client, _ = rig
    assert client.post("/convert/confirm", data={}).status_code == 302
