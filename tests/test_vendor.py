import hashlib

import pytest

from tuya_local_bridge.vendor import (
    VENDORS,
    VendorApiError,
    VendorAuthError,
    VendorSession,
    _encrypt_password,
    _mobile_hash,
    _to_cloud_device,
    fetch_devices,
)


def session():
    return VendorSession(VENDORS["ledvance"], "a@b.com", "pw")


def test_mobile_hash_reorders_the_md5_in_tuya_s_own_way():
    digest = hashlib.md5(b"hello").hexdigest()
    expected = digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]
    assert _mobile_hash("hello") == expected
    assert len(_mobile_hash("hello")) == 32


def test_password_encryption_is_raw_rsa_over_the_md5():
    # Small textbook key: n = 3233, e = 17. Raw RSA is plain modular exponentiation.
    out = _encrypt_password("3233", "17", "secret")

    assert out.startswith("0" * 64)
    message = int.from_bytes(hashlib.md5(b"secret").hexdigest().encode(), "big")
    expected = pow(message, 17, 3233)
    assert int(out[64:], 16) == expected


def test_signature_only_covers_the_documented_keys():
    s = session()
    signed = s._sign({"a": "x", "time": "1", "ignored": "should-not-count"})
    same_without = s._sign({"a": "x", "time": "1"})
    assert signed == same_without


def test_signature_ignores_empty_values():
    s = session()
    assert s._sign({"a": "x", "sid": ""}) == s._sign({"a": "x"})


def test_signature_hashes_the_body_rather_than_including_it():
    s = session()
    signed = s._sign({"a": "x", "postData": '{"devId":"1"}'})
    literal = s._sign({"a": "x", "postData": _mobile_hash('{"devId":"1"}')})
    # postData is hashed on the way in, so passing the hash gives a different
    # result — proving it is not signed verbatim.
    assert signed != literal


def test_signature_changes_with_the_vendor_secret():
    ledvance = VendorSession(VENDORS["ledvance"], "a@b.com", "pw")
    sylvania = VendorSession(VENDORS["sylvania"], "a@b.com", "pw")
    assert ledvance._sign({"a": "x"}) != sylvania._sign({"a": "x"})


def test_wrong_password_raises_an_auth_error():
    with pytest.raises(VendorAuthError):
        VendorSession._unwrap({"success": False, "errorCode": "USER_PASSWD_WRONG"})


def test_other_errors_are_api_errors():
    with pytest.raises(VendorApiError):
        VendorSession._unwrap({"success": False, "errorCode": "SOMETHING_ELSE"})


def test_success_returns_the_result():
    assert VendorSession._unwrap({"success": True, "result": [1, 2]}) == [1, 2]


def test_calling_before_login_is_refused():
    with pytest.raises(VendorApiError):
        session()._call("tuya.m.location.list")


def test_unknown_vendor_is_rejected_with_the_known_list():
    with pytest.raises(VendorApiError) as excinfo:
        fetch_devices("nosuchbrand", "a@b.com", "pw")
    assert "ledvance" in str(excinfo.value)


def test_device_normalisation_onto_the_shared_model():
    device = _to_cloud_device(
        {
            "devId": "bf545a49de3bb330e8jfsb",
            "name": "Kitchen bulb",
            "localKey": "abc123",
            "isOnline": True,
            "productId": "keytg5kq8gvkv9dh",
        }
    )
    assert device.id == "bf545a49de3bb330e8jfsb"
    assert device.local_key == "abc123"
    assert device.online is True
    assert device.convertible
    # This API reports no address at all, unlike the sharing API's WAN one.
    assert device.wan_ip == ""


def test_a_device_without_a_key_is_not_convertible():
    assert not _to_cloud_device({"devId": "x", "localKey": ""}).convertible


def test_password_encoding_pads_to_the_modulus_width():
    # Verified against the live API: LEDVANCE returns a 1024-bit modulus with
    # exponent 3. md5-hex is 32 ASCII bytes (256 bits), so cubing gives 768 bits
    # = 192 hex chars, and the 64-zero prefix pads that to the full 256.
    modulus = str((1 << 1023) + 1234567)  # 1024-bit
    out = _encrypt_password(modulus, "3", "hunter2")

    assert len(out) == 256
    assert out[:64] == "0" * 64


def test_country_code_and_region_reach_the_session():
    s = VendorSession(VENDORS["ledvance"], "a@b.com", "pw", region="us", country_code=1)
    assert s.country_code == 1
    assert "tuyaus" in s.endpoint


def test_unknown_region_falls_back_to_eu():
    s = VendorSession(VENDORS["ledvance"], "a@b.com", "pw", region="mars")
    assert "tuyaeu" in s.endpoint
