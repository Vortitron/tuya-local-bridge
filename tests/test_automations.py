"""Repointing automations that target a device.

An entity id is the reference, so moving it carries everything with it. A
device reference is a uuid, which survives every rename — so a converted
device leaves its automations pointing at the disabled cloud device. They keep
running and do nothing, which is the worst way to fail.
"""
from tuya_local_bridge.automations import (
    automation_ids,
    count_references,
    rewrite_references,
)

CLOUD = "b66e977f1d0c4a1f"
LOCAL = "0e49bb170d30ff40"

# The shape Home Assistant's own automation editor writes.
AUTOMATION = {
    "alias": "Ice machine Midnight flipper",
    "trigger": [{"platform": "time", "at": "23:30:00"}],
    "condition": [
        {"condition": "device", "device_id": CLOUD, "domain": "switch", "type": "is_on"}
    ],
    "action": [
        {"device_id": CLOUD, "domain": "switch", "type": "turn_off"},
        {"delay": {"minutes": 1}},
        {"device_id": CLOUD, "domain": "switch", "type": "turn_on"},
    ],
}


def test_every_reference_is_counted():
    assert count_references(AUTOMATION, CLOUD) == 3


def test_an_automation_that_does_not_mention_it_counts_zero():
    assert count_references(AUTOMATION, "something-else") == 0


def test_references_in_a_target_block_are_found_too():
    # The editor writes device ids into target blocks as well as device_id keys.
    config = {"action": [{"service": "switch.turn_on", "target": {"device_id": [CLOUD]}}]}
    assert count_references(config, CLOUD) == 1


def test_rewriting_replaces_every_one():
    rewritten = rewrite_references(AUTOMATION, CLOUD, LOCAL)

    assert count_references(rewritten, CLOUD) == 0
    assert count_references(rewritten, LOCAL) == 3


def test_rewriting_leaves_everything_else_alone():
    rewritten = rewrite_references(AUTOMATION, CLOUD, LOCAL)

    assert rewritten["alias"] == AUTOMATION["alias"]
    assert rewritten["trigger"] == AUTOMATION["trigger"]
    assert rewritten["action"][1] == {"delay": {"minutes": 1}}
    assert rewritten["condition"][0]["type"] == "is_on"


def test_the_original_is_not_mutated():
    # The caller keeps it to put back if the save fails.
    rewrite_references(AUTOMATION, CLOUD, LOCAL)
    assert count_references(AUTOMATION, CLOUD) == 3


def test_rewriting_to_the_same_id_is_a_no_op():
    assert rewrite_references(AUTOMATION, CLOUD, CLOUD) == AUTOMATION


def test_a_partial_match_is_not_a_reference():
    # Substring matching here would corrupt unrelated ids.
    config = {"action": [{"device_id": CLOUD + "0"}]}
    assert count_references(config, CLOUD) == 0
    assert rewrite_references(config, CLOUD, LOCAL) == config


def test_ids_come_from_the_automation_entities():
    entities = [
        {"platform": "automation", "unique_id": "1744274343050"},
        {"platform": "automation", "unique_id": "1744274430824"},
        {"platform": "light", "unique_id": "not-an-automation"},
        {"platform": "automation"},
        "junk",
    ]
    assert automation_ids(entities) == ["1744274343050", "1744274430824"]


def test_no_automations_is_not_an_error():
    assert automation_ids([]) == []
    assert automation_ids(None) == []
