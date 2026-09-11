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
    html = _render_swap_preview([("abc", _plan(PAIR))], [])

    assert "light.front_porch_local" in html
    assert "light.front_porch" in html
    # The reassurance that makes it safe to press: nothing else has to change.
    assert "keeps working untouched" in html


def test_the_preview_warns_about_entities_left_on_the_cloud():
    # A plug's main switch often does not pair, and anything using it keeps
    # going through Tuya. Silence there would be the worst outcome.
    html = _render_swap_preview(
        [("abc", _plan(PAIR, cloud_unmatched=["switch.plug_socket_1"]))], []
    )

    assert "switch.plug_socket_1" in html
    assert "Left on the cloud" in html


def test_the_preview_offers_nothing_to_press_when_there_is_nothing_to_do():
    html = _render_swap_preview([], [("abc", "nothing pairs up to swap")])

    assert "Cannot swap" in html
    assert "nothing pairs up to swap" in html
    assert "<button>" not in html


def test_devices_carry_through_the_preview_to_the_apply_form():
    html = _render_swap_preview([("bf1000aa2000bb3000ccd1", _plan(PAIR))], [])
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
    html = _render_swap_preview([("abc", _plan(pair))], [])

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


def test_a_device_whose_ids_already_moved_is_not_offered_again():
    html = _render_status(converted_result("abc"), {}, [], swapped=frozenset({"abc"}))

    assert 'name="device" value="abc"' not in html
    assert "ids moved" in html


def test_the_button_disappears_when_every_device_is_done():
    html = _render_status(converted_result("abc"), {}, [], swapped=frozenset({"abc"}))
    assert "Move the entity ids across" not in html


def test_a_mix_offers_only_the_ones_still_needing_it():
    html = _render_status(
        converted_result("abc", "def"), {}, [], swapped=frozenset({"abc"})
    )

    assert 'name="device" value="def"' in html
    assert 'name="device" value="abc"' not in html
    assert "Move the entity ids across" in html


def test_the_section_says_why_it_matters():
    html = _render_status(converted_result("abc"), {}, [])
    assert "still pointing at the" in html
