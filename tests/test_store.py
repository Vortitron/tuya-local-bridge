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


class TestKeysWeCannotCheck:
    """A vendor key is read once and never re-read.

    The Smart Life key is refreshed on every sync, so a rotated one is caught
    within minutes. A LEDVANCE key is read when somebody types that account's
    password and never again — so a re-paired device rotates its key and the
    only symptom is the device going quiet. We cannot detect that, but we can
    say which account would settle it.
    """

    def store_with_both(self, tmp_path, now=0.0):
        from tuya_local_bridge.store import SMART_LIFE, ProvenanceStore

        store = ProvenanceStore(str(tmp_path / "p.json"))
        store.record_cloud([cloud(id_="own", key="k1")], now=now, source=SMART_LIFE)
        store.record_cloud(
            [cloud(id_="gold1", name="gold light 1", key="k2")],
            now=now,
            source="ledvance",
            account="you@example.com",
        )
        return store

    def test_a_fresh_vendor_key_raises_nothing(self, tmp_path):
        store = self.store_with_both(tmp_path, now=1000.0)
        assert store.unverifiable_keys(max_age_seconds=100.0, now=1050.0) == {}

    def test_an_old_vendor_key_is_grouped_by_its_account(self, tmp_path):
        store = self.store_with_both(tmp_path, now=0.0)

        groups = store.unverifiable_keys(max_age_seconds=100.0, now=1000.0)

        assert list(groups) == [("ledvance", "you@example.com")]
        assert [r.device_id for r in groups[("ledvance", "you@example.com")]] == ["gold1"]

    def test_the_smart_life_account_is_never_listed(self, tmp_path):
        # It is re-read on every refresh, so it is never in doubt.
        store = self.store_with_both(tmp_path, now=0.0)
        groups = store.unverifiable_keys(max_age_seconds=100.0, now=1000.0)

        assert all(source != "smartlife" for source, _ in groups)

    def test_a_key_with_no_recorded_source_is_left_alone(self, tmp_path):
        # Records written before sources were tracked must not start nagging.
        from tuya_local_bridge.store import ProvenanceStore

        store = ProvenanceStore(str(tmp_path / "p.json"))
        store.record_cloud([cloud(id_="old", key="k")], now=0.0)

        assert store.unverifiable_keys(max_age_seconds=100.0, now=1000.0) == {}

    def test_signing_in_again_clears_it(self, tmp_path):
        store = self.store_with_both(tmp_path, now=0.0)
        assert store.unverifiable_keys(100.0, now=1000.0)

        store.record_cloud(
            [cloud(id_="gold1", name="gold light 1", key="k2")],
            now=1000.0,
            source="ledvance",
            account="you@example.com",
        )
        assert store.unverifiable_keys(100.0, now=1050.0) == {}

    def test_a_rotated_vendor_key_is_still_reported_as_rotated(self, tmp_path):
        store = self.store_with_both(tmp_path, now=0.0)

        rotated = store.record_cloud(
            [cloud(id_="gold1", name="gold light 1", key="NEW")],
            now=1000.0,
            source="ledvance",
            account="you@example.com",
        )
        assert rotated == ["gold1"]
