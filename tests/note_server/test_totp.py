from __future__ import annotations

import binascii

import pytest

from note_server.totp import (
    SECRET_B32,
    STEP_SECONDS,
    decode_secret,
    totp,
    verify,
)

# A large, realistic fixed timestamp (avoids the pre-epoch counter edge).
FIXED_NOW = 1_000_000_000.0
KEY = decode_secret(SECRET_B32)


def test_decode_secret_normalizes_and_pads() -> None:
    # The hardcoded secret decodes cleanly to a known 13-byte value.
    assert decode_secret(SECRET_B32) == b"Skeev-Okinawa"
    # Lower-case + surrounding whitespace normalize to the same bytes.
    assert decode_secret("  knvwkzlwfvhww2lomf3wc  ") == b"Skeev-Okinawa"


def test_decode_secret_rejects_non_base32() -> None:
    with pytest.raises(binascii.Error):
        decode_secret("not-base32!!!")


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [(0, "411400"), (59, "795119"), (1234567890, "948172")],
)
def test_totp_reference_vectors(timestamp: int, expected: str) -> None:
    assert totp(KEY, timestamp) == expected


def test_totp_rejects_pre_epoch_timestamp() -> None:
    with pytest.raises(ValueError):
        totp(KEY, -1)


def test_verify_accepts_current_window() -> None:
    code = totp(KEY, FIXED_NOW)
    assert verify(code, KEY, now=FIXED_NOW) is True


def test_verify_accepts_plus_and_minus_one_step() -> None:
    prev = totp(KEY, FIXED_NOW - STEP_SECONDS)
    nxt = totp(KEY, FIXED_NOW + STEP_SECONDS)
    assert verify(prev, KEY, now=FIXED_NOW) is True
    assert verify(nxt, KEY, now=FIXED_NOW) is True


def test_verify_rejects_plus_and_minus_two_steps() -> None:
    two_back = totp(KEY, FIXED_NOW - 2 * STEP_SECONDS)
    two_fwd = totp(KEY, FIXED_NOW + 2 * STEP_SECONDS)
    assert verify(two_back, KEY, now=FIXED_NOW) is False
    assert verify(two_fwd, KEY, now=FIXED_NOW) is False


def test_verify_rejects_wrong_code() -> None:
    assert verify("000000", KEY, now=FIXED_NOW) is False


def test_verify_rejects_non_ascii_token_without_raising() -> None:
    # A non-ASCII token must be a clean rejection, not a TypeError crash.
    assert verify("１２３４５６", KEY, now=FIXED_NOW) is False


def test_verify_near_epoch_skips_negative_counter() -> None:
    # now=0 -> the c-1 neighbour is counter -1; verify must skip it, not crash.
    code = totp(KEY, 0)
    assert verify(code, KEY, now=0) is True
