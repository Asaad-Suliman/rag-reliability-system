"""Prefixed, lexicographically sortable IDs.

A ULID is a 128-bit value: a 48-bit millisecond timestamp followed by 80 bits of
randomness, both rendered in Crockford base32. Sorting the string sorts by
creation time — Step 04's cursor pagination is a plain `WHERE id > :cursor`, no
extra `created_at` tiebreaker column needed. The prefix (`doc_`, `chk_`, ...)
makes an ID self-describing in logs and error messages.

Not `python-ulid`: the whole algorithm is os.urandom + base32, and a dependency
buys nothing a stdlib-based helper does not already give us.
"""

from __future__ import annotations

import os
import time

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_id(prefix: str) -> str:
    """`{prefix}_{26-char ULID}`, e.g. `doc_01J8Z3K4QZXVN8P5R6T7W8Y9ZA`."""
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), byteorder="big")
    ulid = _encode_base32(timestamp_ms, 10) + _encode_base32(randomness, 16)
    return f"{prefix}_{ulid}"


def _demo() -> None:
    a = new_id("doc")
    time.sleep(0.002)
    b = new_id("doc")
    assert a.startswith("doc_") and len(a) == 4 + 26, a
    assert a < b, "later ID must sort after earlier ID"
    assert new_id("doc") != new_id("doc"), "two IDs must not collide"
    print("ok:", a, b)


if __name__ == "__main__":
    _demo()
