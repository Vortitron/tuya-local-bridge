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


# ── Moving entity ids across after a conversion ────────────────────────────

from tuya_local_bridge.swap import EntityPair, SwapPlan  # noqa: E402
from tuya_local_bridge.web import _render_swap_preview, _render_swap_result  # noqa: E402


def _plan(*pairs, cloud_unmatched=()):
    plan = SwapPlan()
    plan.pairs = list(pairs)
    plan.cloud_unmatched = list(cloud_unmatched)
    return plan


PAIR = EntityPair(
    cloud_entity_id="light.front_porch",
    local_entity_id="light.front_porch_local",
    domain="light",
    name=None,
)


def test_the_preview_says_which_id_moves_where():
    html = _render_swap_preview([("abc", _plan(PAIR), "cloud_dev", "local_dev")], [])

    assert "light.front_porch_local" in html
    assert "light.front_porch" in html
    # The reassurance that makes it safe to press: nothing else has to change.
    assert "keeps working untouched" in html


def test_the_preview_warns_about_entities_left_on_the_cloud():
    # A plug's main switch often does not pair, and anything using it keeps
    # going through Tuya. Silence there would be the worst outcome.
    html = _render_swap_preview(
        [("abc", _plan(PAIR, cloud_unmatched=["switch.plug_socket_1"]), "cloud_dev", "local_dev")],
        [],
    )

    assert "switch.plug_socket_1" in html
    assert "Left on the cloud" in html


def test_the_preview_offers_nothing_to_press_when_there_is_nothing_to_do():
    html = _render_swap_preview([], [("abc", "nothing pairs up to swap")])

    assert "Cannot swap" in html
    assert "nothing pairs up to swap" in html
    assert "<button>" not in html


def test_devices_carry_through_the_preview_to_the_apply_form():
    html = _render_swap_preview(
        [("bf1000aa2000bb3000ccd1", _plan(PAIR), "cloud_dev", "local_dev")], []
    )
    assert 'name="device" value="bf1000aa2000bb3000ccd1"' in html


def test_the_result_mentions_how_to_undo_it():
    html = _render_swap_result([("light.front_porch", "was light.front_porch_local")], [])
    assert "rollback" in html


def test_a_failure_is_shown_rather_than_swallowed():
    html = _render_swap_result([], [("light.front_porch", "boom")])
    assert "failed" in html
    assert "boom" in html
    # Nothing moved, so do not advertise an undo that has nothing to undo.
    assert "rollback" not in html


def test_swap_preview_escapes_entity_ids():
    pair = EntityPair(
        cloud_entity_id='light.<script>alert("x")</script>',
        local_entity_id="light.a",
        domain="light",
        name=None,
    )
    html = _render_swap_preview([("abc", _plan(pair), "cloud_dev", "local_dev")], [])

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── Fixing devices converted before the offer existed ──────────────────────


def converted_result(*ids):
    """A reconciliation where the given devices are already on tuya-local."""
    devices = [cloud(id_=i, name=f"device {i}") for i in ids]
    return reconcile(devices, [], already_converted=set(ids))


def test_already_converted_devices_can_be_selected_for_a_swap():
    # The whole point: someone who converted last week should not have to
    # convert again to get their automations pointing at the right entity.
    html = _render_status(converted_result("abc"), {}, [])

    assert 'name="device" value="abc"' in html
    assert "Move the entity ids across" in html


def test_a_device_whose_ids_moved_is_labelled_as_such():
    # It stays selectable -- it has to be, to be undone -- but the list says
    # which state it is in rather than inviting the same action blindly.
    html = _render_status(converted_result("abc"), {}, [], swapped=frozenset({"abc"}))

    assert 'name="device" value="abc"' in html
    assert "ids moved" in html


def test_undo_is_offered_only_once_something_has_moved():
    plain = _render_status(converted_result("abc"), {}, [])
    assert "Undo an id move" not in plain

    moved = _render_status(converted_result("abc"), {}, [], swapped=frozenset({"abc"}))
    assert "Undo an id move" in moved


def test_both_actions_share_one_form_so_the_selection_serves_either():
    html = _render_status(converted_result("abc", "def"), {}, [], swapped=frozenset({"abc"}))

    assert 'name="device" value="abc"' in html
    assert 'name="device" value="def"' in html
    assert "Move the entity ids across" in html
    assert "formaction" in html


def test_the_section_says_why_it_matters():
    html = _render_status(converted_result("abc"), {}, [])
    assert "still pointing at the" in html


# ── Saying what is happening while the network is scanned ──────────────────


def test_a_running_scan_says_so_with_a_counter():
    # A still "Scanning..." with no number is exactly what a hung page looks
    # like, which is the complaint this replaces.
    html = _render_status(
        reconcile([], []), {}, [], scan_enabled=True, scanning=True, scan_elapsed=12
    )

    assert "Scanning the network" in html
    assert "12s" in html
    assert "spinner" in html


def test_a_running_scan_does_not_also_offer_a_rescan():
    html = _render_status(reconcile([], []), {}, [], scan_enabled=True, scanning=True)
    assert "Rescan the network" not in html


def test_a_failed_scan_explains_what_still_works():
    html = _render_status(
        reconcile([], []), {}, [], scan_enabled=True, scan_error="the network could not be scanned"
    )

    assert "network scan failed" in html
    assert "already converted may be" in html


def test_a_finished_scan_offers_a_rescan_that_does_not_block():
    html = _render_status(reconcile([], []), {}, [], scan_enabled=True)

    assert "Rescan the network" in html
    assert "runs in the background" in html


def test_scan_errors_are_escaped():
    html = _render_status(
        reconcile([], []), {}, [], scan_enabled=True, scan_error='<script>alert("x")</script>'
    )
    assert "<script>" not in html


# ── Asking, where the matcher will not guess ───────────────────────────────


def _plan_with_choice():
    """A plug: two switches each side, names that do not correspond."""
    from tuya_local_bridge.swap import plan_swap

    return plan_swap(
        [
            {"entity_id": "switch.plug_socket_1", "original_name": "Socket 1"},
            {"entity_id": "switch.plug_child_lock", "original_name": "Child lock"},
        ],
        [
            {"entity_id": "switch.hot_water", "original_name": None},
            {"entity_id": "switch.hot_water_overcharge", "original_name": "Overcharge protection"},
        ],
    )


def test_undecidable_entities_become_a_question():
    html = _render_swap_preview([("abc", _plan_with_choice(), "cloud_dev", "local_dev")], [])

    assert "These need you to decide" in html
    assert 'name="pair__switch.plug_socket_1"' in html
    # Both local switches must be offered as options.
    assert "switch.hot_water_overcharge" in html
    assert ">leave it on the cloud<" in html


def test_the_question_and_the_button_are_in_one_form():
    # A choice made here has to travel with the button that applies it.
    html = _render_swap_preview([("abc", _plan_with_choice(), "cloud_dev", "local_dev")], [])

    assert html.count("<form") == 1
    assert html.index("pair__switch.plug_socket_1") < html.index("<button>")


def test_a_device_needing_only_choices_still_offers_the_button():
    html = _render_swap_preview([("abc", _plan_with_choice(), "cloud_dev", "local_dev")], [])
    assert "Move the ids" in html


def test_entities_with_no_candidate_at_all_are_not_offered_a_dropdown():
    from tuya_local_bridge.swap import plan_swap

    plan = plan_swap([{"entity_id": "sensor.only_cloud", "original_name": "Signal"}], [])
    html = _render_swap_preview([("abc", plan, "cloud_dev", "local_dev")], [])

    assert "Left on the cloud" in html
    assert "pair__" not in html


def test_one_devices_choice_is_not_offered_to_another(monkeypatch, tmp_path):
    """The form carries every selected device's answers together.

    Handing device B a choice made for device A made B refuse a pairing never
    addressed to it, and report it as a failure beside A's successful move.
    """
    import tuya_local_bridge.web as web
    from tuya_local_bridge.swap import plan_swap

    plan_a = plan_swap(
        [
            {"entity_id": "switch.a_socket", "original_name": "Socket 1"},
            {"entity_id": "switch.a_lock", "original_name": "Child lock"},
        ],
        [
            {"entity_id": "switch.a_local", "original_name": None},
            {"entity_id": "switch.a_over", "original_name": "Overcharge"},
        ],
    )
    plan_b = plan_swap(
        [
            {"entity_id": "switch.b_socket", "original_name": "Socket 1"},
            {"entity_id": "switch.b_lock", "original_name": "Child lock"},
        ],
        [
            {"entity_id": "switch.b_local", "original_name": None},
            {"entity_id": "switch.b_over", "original_name": "Overcharge"},
        ],
    )

    choices = {"switch.a_socket": "switch.a_local"}
    refusals = []
    for plan in (plan_a, plan_b):
        mine = {c: v for c, v in choices.items() if c in plan.cloud_unmatched}
        refusals.extend(web.add_manual_pairs(plan, mine))

    assert refusals == [], "a choice for another device must not be refused here"
    assert len(plan_a.pairs) == 1
    assert plan_b.pairs == []


# ── Undoing an id move, and other brands, from the interface ───────────────

from tuya_local_bridge.store import Migration  # noqa: E402
from tuya_local_bridge.web import (  # noqa: E402
    _render_rollback_preview,
    _render_rollback_result,
)


def _migration():
    return Migration(
        cloud_entity_id="light.front_porch",
        local_entity_id="light.front_porch",
        local_entity_id_original="light.front_porch_local",
        migrated_at=1.0,
    )


def test_the_undo_preview_says_where_each_id_goes_back_to():
    html = _render_rollback_preview([("abc", "Front Porch", [_migration()])], [])

    assert "light.front_porch" in html
    assert "light.front_porch_local" in html
    assert "Put the ids back" in html


def test_the_undo_preview_warns_it_returns_you_to_the_cloud():
    # The consequence people will not have thought about.
    html = _render_rollback_preview([("abc", "Front Porch", [_migration()])], [])
    assert "talking to the cloud again" in html


def test_a_device_with_nothing_recorded_offers_no_undo_button():
    html = _render_rollback_preview([], [("abc", "no id move on record to undo")])

    assert "Put the ids back" not in html
    assert "no id move on record to undo" in html


def test_the_undo_result_reports_failures():
    html = _render_rollback_result([], [("light.a", "boom")])
    assert "failed" in html
    assert "boom" in html


def test_the_unexplained_section_points_at_the_vendor_login():
    from tuya_local_bridge.models import LanDevice

    result = reconcile([], [LanDevice(id="ghost", ip="192.168.1.99")])
    html = _render_status(result, {}, [])

    assert "another brand" in html
    assert "/vendor" in html


def test_stored_vendor_keys_join_the_picture(tmp_path):
    from tuya_local_bridge.models import CloudDevice
    from tuya_local_bridge.store import ProvenanceStore
    from tuya_local_bridge.web import stored_devices

    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([CloudDevice(id="ledvance1", name="gold light 1", local_key="k")], now=1.0)

    extra = stored_devices(store, known={"smartlife1"})

    assert [d.id for d in extra] == ["ledvance1"]
    assert extra[0].local_key == "k"


def test_devices_already_in_this_session_are_not_duplicated(tmp_path):
    from tuya_local_bridge.models import CloudDevice
    from tuya_local_bridge.store import ProvenanceStore
    from tuya_local_bridge.web import stored_devices

    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud([CloudDevice(id="abc", name="x", local_key="k")], now=1.0)

    assert stored_devices(store, known={"abc"}) == []


def test_every_page_agrees_about_which_devices_exist(tmp_path, monkeypatch):
    """A device listed on one page must still exist on the next.

    Vendor-account keys were folded in only on the status page, so a LEDVANCE
    bulb could be listed as ready, selected, and then met with "nothing
    selected" — because the page that acted rebuilt the picture from the Smart
    Life session alone and the device was simply not in it.
    """
    import tuya_local_bridge.web as web
    from tuya_local_bridge.models import CloudDevice
    from tuya_local_bridge.store import ProvenanceStore

    store = ProvenanceStore(str(tmp_path / "p.json"))
    store.record_cloud(
        [
            CloudDevice(id="smartlife1", name="bedroom light", local_key="k1"),
            CloudDevice(id="ledvance1", name="gold light 1", local_key="k2"),
        ],
        now=1.0,
    )
    store.save()

    session_devices = [CloudDevice(id="smartlife1", name="bedroom light", local_key="k1")]
    folded = session_devices + web.stored_devices(store, {d.id for d in session_devices})

    ids = {d.id for d in folded}
    assert "ledvance1" in ids, "the vendor device must survive the rebuild"
    assert "smartlife1" in ids
    assert len(folded) == 2, "and must not be duplicated"


def test_unverifiable_keys_are_explained_with_a_way_to_fix_them():
    from tuya_local_bridge.store import DeviceRecord

    records = [DeviceRecord(device_id="g1", name="gold light 1")]
    html = _render_status(
        reconcile([], []),
        {},
        [],
        unverifiable={("ledvance", "you@example.com"): records},
    )

    assert "cannot be checked from here" in html
    assert "gold light 1" in html
    # And the link must carry both, or the offer is just a nag.
    assert "vendor=ledvance" in html
    assert "email=you@example.com" in html


def test_no_prompt_when_every_key_is_verifiable():
    html = _render_status(reconcile([], []), {}, [], unverifiable={})
    assert "cannot be checked from here" not in html


def test_a_long_list_is_summarised_rather_than_dumped():
    from tuya_local_bridge.store import DeviceRecord

    records = [DeviceRecord(device_id=f"g{i}", name=f"gold light {i}") for i in range(9)]
    html = _render_status(reconcile([], []), {}, [], unverifiable={("ledvance", ""): records})

    assert "and 3 more" in html


def test_devices_whose_names_never_followed_are_offered_a_catch_up():
    html = _render_status(
        reconcile([], []),
        {},
        [],
        needing_rename=[("bf2d4fc9", "ice ice machine"), ("bfed759f", "hot water")],
    )

    assert "Device names have not followed" in html
    assert "ice ice machine" in html
    assert "Move the device names too" in html
    assert "/rename" in html


def test_no_catch_up_offered_when_nothing_is_half_done():
    html = _render_status(reconcile([], []), {}, [], needing_rename=[])
    assert "Device names have not followed" not in html


# ── Automations that target a device rather than an entity ─────────────────

from tuya_local_bridge.web import _render_automations_preview  # noqa: E402

FOUND = [
    (
        "bf2d4fc9",
        "ice ice machine",
        [("1744274343050", "Ice machine Midnight flipper", 3)],
    )
]


def test_the_preview_names_the_automations_and_counts_references():
    html = _render_automations_preview(FOUND)

    assert "Ice machine Midnight flipper" in html
    assert "3 references" in html
    assert "ice ice machine" in html


def test_the_preview_explains_why_a_rename_did_not_fix_it():
    html = _render_automations_preview(FOUND)

    assert "no rename can follow" in html
    assert "doing nothing" in html


def test_the_preview_promises_an_undo_before_asking_to_proceed():
    html = _render_automations_preview(FOUND)

    assert "saved whole before it is changed" in html
    assert html.index("saved whole") < html.index("<button>")


def test_nothing_to_do_says_so_and_why():
    html = _render_automations_preview([])

    assert "No automation refers to a converted device by device" in html
    assert "<button>" not in html


def test_the_status_page_offers_it_when_something_is_stale():
    html = _render_status(reconcile([], []), {}, [], stale_automations=FOUND)

    assert "still point at the cloud" in html
    assert "Show me which" in html


def test_the_status_page_stays_quiet_when_nothing_is_stale():
    html = _render_status(reconcile([], []), {}, [], stale_automations=[])
    assert "still point at the cloud" not in html


def test_automation_aliases_are_escaped():
    found = [("d", "dev", [("1", '<script>alert("x")</script>', 1)])]
    html = _render_automations_preview(found)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_converted_devices_offer_a_resync():
    """A device can be plainly reachable and its entry still refuse to load."""
    html = _render_status(converted_result("abc"), {}, [])

    assert "Re-sync with tuya-local" in html
    assert "/resync" in html


def test_a_forced_rescan_does_not_stay_in_the_address_bar(tmp_path):
    """The refresh re-requests the current URL.

    Leaving ?rescan=1 on it restarted the scan every six seconds for ever,
    which is what a counter going 16s, 0s, 16s looks like from outside.
    """
    import logging

    logging.disable(logging.CRITICAL)
    from tuya_local_bridge.web import create_app

    app = create_app(str(tmp_path), ha_url="http://ha", ha_token="t", scan_seconds=0)
    # No session, so index redirects to login; what matters is that ?rescan=1
    # is answered with a redirect rather than a rendered, refreshing page.
    response = app.test_client().get("/?rescan=1")

    assert response.status_code in (302, 303)


# ── Asking which device a converted one replaces ───────────────────────────

from tuya_local_bridge.web import _render_swap_questions  # noqa: E402


def _question():
    return [(
        "bf1ca00f2963c8dab2mvya",
        "gold light 1",
        [{"id": "st1", "name": "gold light 1"}, {"id": "st2", "name": "gold light 4"}],
    )]


def test_a_device_with_no_twin_is_asked_about_rather_than_refused():
    """"Needs both a cloud and a local device" is not something anyone can act on.

    A LEDVANCE bulb arrives through SmartThings, which shares no id with Tuya,
    so the pairing cannot be found — but the device is plainly there and the
    owner knows which it is.
    """
    html = _render_swap_questions(_question())

    assert 'name="replaces__bf1ca00f2963c8dab2mvya"' in html
    assert "gold light 1" in html
    assert "<button>" in html


def test_the_answer_can_be_that_there_is_no_predecessor():
    # A device genuinely new to Home Assistant has nothing to take over from,
    # and must not be forced into swapping with whatever ranked first.
    assert 'value=""' in _render_swap_questions(_question())


def test_the_question_is_shown_even_when_nothing_else_can_move():
    html = _render_swap_preview([], [], _question())

    assert 'name="replaces__bf1ca00f2963c8dab2mvya"' in html
    assert "Nothing to swap" not in html


def test_the_chosen_predecessor_travels_to_the_apply_form():
    # The preview re-plans from the answer; the apply step must not have to
    # guess it again, or it would fall back to "no twin" and refuse.
    html = _render_swap_preview([("abc", _plan(PAIR), "cloud_dev", "local_dev")], [])
    assert 'name="replaces__abc" value="cloud_dev"' in html
