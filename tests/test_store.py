import json
import os

from tuya_local_bridge.models import CloudDevice, LanDevice
from tuya_local_bridge.store import ProvenanceStore


def cloud(id_="abc", key="key1", name="bulb"):
    return CloudDevice(id=id_, name=name, local_key=key)


def test_first_sync_records_the_key(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    assert store.record_cloud([cloud()], now=100.0) == []

    rec = store.get("abc")
    assert rec.local_key == "key1"
    assert rec.key_generation == 1
    assert rec.key_first_seen == 100.0


def test_rotation_is_detected_and_generation_bumped(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([cloud(key="key1")], now=100.0)

    rotated = store.record_cloud([cloud(key="key2")], now=200.0)

    assert rotated == ["abc"]
    rec = store.get("abc")
    assert rec.local_key == "key2"
    assert rec.key_generation == 2
    assert rec.key_rotated_at == 200.0


def test_unchanged_key_refreshes_confirmation_without_rotating(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([cloud(key="key1")], now=100.0)

    assert store.record_cloud([cloud(key="key1")], now=500.0) == []
    rec = store.get("abc")
    assert rec.key_generation == 1
    assert rec.key_last_confirmed == 500.0


def test_round_trips_through_disk(tmp_path):
    path = str(tmp_path / "p.json")
    store = ProvenanceStore(path)
    store.record_cloud([cloud()], now=100.0)
    store.record_lan([LanDevice(id="abc", ip="192.168.1.5", version="3.5")], now=110.0)
    store.record_migration("abc", "light.bulb", "light.bulb_2", "light.bulb_2", now=120.0)
    store.save()

    reloaded = ProvenanceStore(path)
    rec = reloaded.get("abc")
    assert rec.local_key == "key1"
    assert rec.last_lan_ip == "192.168.1.5"
    assert rec.protocol_version == "3.5"
    assert rec.active_migration.cloud_entity_id == "light.bulb"


def test_saved_file_is_not_world_readable(tmp_path):
    # The file holds local keys, which are device credentials.
    path = str(tmp_path / "p.json")
    store = ProvenanceStore(path)
    store.record_cloud([cloud()], now=100.0)
    store.save()

    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_rollback_clears_the_active_migration(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_migration("abc", "light.bulb", "light.bulb_2", now=120.0)
    assert store.get("abc").active_migration is not None

    store.record_rollback("abc", now=130.0)

    rec = store.get("abc")
    assert rec.active_migration is None
    assert rec.migrations[0].rolled_back_at == 130.0


def test_stale_keys_only_covers_migrated_devices(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([cloud(id_="migrated"), cloud(id_="untouched")], now=0.0)
    store.record_migration("migrated", "light.a", "light.a_2", now=0.0)

    stale = store.stale_keys(max_age_seconds=10.0, now=1000.0)

    assert [r.device_id for r in stale] == ["migrated"]


def test_fresh_keys_are_not_stale(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([cloud()], now=990.0)
    store.record_migration("abc", "light.a", "light.a_2", now=990.0)

    assert store.stale_keys(max_age_seconds=100.0, now=1000.0) == []


def test_lan_sighting_for_unknown_device_creates_a_record(tmp_path):
    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_lan([LanDevice(id="ghost", ip="192.168.1.9")], now=100.0)

    rec = store.get("ghost")
    assert rec.last_lan_ip == "192.168.1.9"
    assert rec.local_key == ""


def test_refuses_a_file_from_a_newer_schema(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"schema_version": 99, "devices": {}}))

    try:
        ProvenanceStore(str(path))
    except RuntimeError as exc:
        assert "newer version" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")
