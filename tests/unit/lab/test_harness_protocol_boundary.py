"""Spikes share wire-level helpers through ``_harness.protocol``, never by
importing another spike's package (0.2.7 S3 review follow-up).

Only ``_s1`` may import from ``_s1``: every other spike package and CLI must
take framing / address rules from :mod:`spikes._harness.protocol`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import spikes._harness.protocol as harness_protocol
import spikes._s1.protocol as s1_protocol

SPIKES_DIR = Path(harness_protocol.__file__).resolve().parents[1]
_S1_IMPORT = re.compile(r"^\s*(from|import)\s+spikes\._s1\b", re.MULTILINE)

OTHER_SPIKE_FILES = sorted(
    p
    for p in SPIKES_DIR.rglob("*.py")
    if "_s1" not in p.parts
    and "_harness" not in p.parts
    and p.name != "s1_network.py"
    and "__pycache__" not in p.parts
)


@pytest.mark.parametrize(
    "path", OTHER_SPIKE_FILES, ids=lambda p: str(p.relative_to(SPIKES_DIR))
)
def test_no_spike_other_than_s1_imports_s1_internals(path: Path) -> None:
    assert not _S1_IMPORT.search(path.read_text(encoding="utf-8")), (
        f"{path.relative_to(SPIKES_DIR)} imports spikes._s1 - use spikes._harness.protocol"
    )


def test_s1_re_exports_the_very_same_harness_objects() -> None:
    """S1's own modules and tests keep importing from ``_s1.protocol``; those
    names must be the harness objects themselves, not copies that could drift."""
    for name in (
        "ProtocolError",
        "recv_exact",
        "send_msg",
        "recv_msg",
        "body_length",
        "decode_body",
        "is_lan_address",
        "is_allowed_peer",
        "check_bind_address",
        "family_of",
        "MAX_HEADER_BYTES",
        "LENGTH_BYTES",
    ):
        assert getattr(s1_protocol, name) is getattr(harness_protocol, name), name
