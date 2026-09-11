"""How the CLI decides where Home Assistant is, and by which road.

The bridge works with no VomeHome account at all. That is easy to break by
accident, because the brokered path was written first — at one point direct
users silently lost the "already converted" bucket, which makes a converted
device look offline. These tests pin the resolution order.
"""
import argparse

import pytest

from tuya_local_bridge.cli import _ha_target


def args(**kwargs):
    defaults = {
        "source": "ha",
        "ha_url": None,
        "token": None,
        "instance": None,
        "api_url": "https://vome.io",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    for name in (
        "HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN",
        "VOMEHOME_INSTANCE_ID", "VOMEHOME_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_a_plain_install_goes_direct():
    target = _ha_target(args(ha_url="http://homeassistant:8123", token="abc"))

    assert target.kind == "direct"
    assert target.url == "http://homeassistant:8123"
    assert target.token == "abc"


def test_the_addon_environment_goes_direct(monkeypatch):
    # This is exactly what the Supervisor gives the add-on.
    monkeypatch.setenv("HA_URL", "http://supervisor/core")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")

    target = _ha_target(args())

    assert target.kind == "direct"
    assert target.token == "supervisor-token"


def test_vomehome_is_used_when_it_is_the_only_thing_configured(monkeypatch):
    monkeypatch.setenv("VOMEHOME_INSTANCE_ID", "rly-1")
    monkeypatch.setenv("VOMEHOME_TOKEN", "vh-token")

    target = _ha_target(args())

    assert target.kind == "vomehome"
    assert target.instance == "rly-1"


def test_direct_wins_when_both_are_configured(monkeypatch):
    monkeypatch.setenv("VOMEHOME_INSTANCE_ID", "rly-1")
    monkeypatch.setenv("VOMEHOME_TOKEN", "vh-token")

    target = _ha_target(args(ha_url="http://homeassistant:8123", token="abc"))

    assert target.kind == "direct"


def test_each_road_can_be_forced(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://homeassistant:8123")
    monkeypatch.setenv("HA_TOKEN", "abc")
    monkeypatch.setenv("VOMEHOME_INSTANCE_ID", "rly-1")
    monkeypatch.setenv("VOMEHOME_TOKEN", "vh-token")

    assert _ha_target(args(source="vomehome")).kind == "vomehome"
    assert _ha_target(args(source="ha-direct")).kind == "direct"


def test_no_credentials_is_a_clear_exit():
    with pytest.raises(SystemExit) as excinfo:
        _ha_target(args())
    # The message has to name what to pass; "cannot reach" alone helps nobody.
    assert "--ha-url" in str(excinfo.value)


def test_forcing_vomehome_without_its_credentials_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://homeassistant:8123")
    monkeypatch.setenv("HA_TOKEN", "abc")

    with pytest.raises(SystemExit):
        _ha_target(args(source="vomehome"))
