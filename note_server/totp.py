"""RFC 6238 TOTP verifier, standard library only.

HMAC-SHA1, 6 digits, 30-second step, with a ±1 step drift window. The current
time is injected (``now``) so the window logic is deterministically testable.
The shared secret is hardcoded per spec (single-user design) - see the "Known
limitation" note in ``note_server/README.md``: this is effectively a shared
static credential, not per-user auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct

# Base32 shared TOTP secret, hardcoded per spec (decodes to b"Skeev-Okinawa").
SECRET_B32 = "KNVWKZLWFVHWW2LOMF3WC"  # gitleaks:allow

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1


def decode_secret(b32: str) -> bytes:
    """Decode a base32 secret, normalizing case and re-padding to a multiple of 8.

    Raises ``binascii.Error`` on characters outside the base32 alphabet.
    """
    normalized = b32.strip().upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    return base64.b32decode(padded)


def _hotp(key: bytes, counter: int, digits: int) -> str:
    """RFC 4226 HOTP for a single non-negative counter value."""
    if counter < 0:
        raise ValueError("counter must be non-negative")
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(binary % (10**digits)).zfill(digits)


def totp(
    key: bytes, timestamp: float, *, step: int = STEP_SECONDS, digits: int = DIGITS
) -> str:
    """TOTP code for ``timestamp`` (seconds since the Unix epoch).

    Raises ``ValueError`` on a pre-epoch (negative) timestamp.
    """
    counter = int(timestamp) // step
    return _hotp(key, counter, digits)


def verify(
    token: str,
    key: bytes,
    *,
    now: float,
    drift: int = DRIFT_STEPS,
    step: int = STEP_SECONDS,
    digits: int = DIGITS,
) -> bool:
    """Return True iff ``token`` matches the code for the current window or ±``drift``.

    Constant-time per candidate via ``hmac.compare_digest``. A malformed token
    (non-ASCII, so unmatchable against the ASCII-digit code) is rejected without
    raising, so the caller always gets a clean auth failure rather than a crash.
    """
    if not token.isascii():
        return False
    counter = int(now) // step
    matched = False
    for delta in range(-drift, drift + 1):
        candidate_counter = counter + delta
        if candidate_counter < 0:
            # struct.pack(">Q", -1) would raise; a pre-epoch window is never valid.
            continue
        candidate = _hotp(key, candidate_counter, digits)
        # Bitwise-or keeps the loop from short-circuiting (no early-return timing
        # signal on which window matched); still cheap for a 3-element window.
        matched |= hmac.compare_digest(token, candidate)
    return matched
