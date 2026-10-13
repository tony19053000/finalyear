# ruff: noqa: E501, T201
"""Tester r3 (V0.7 G6): the redactor against an independent brute-force oracle
(``tests/_support/mf_redaction_oracle.py``, written from the documented contract).

Long runs: ``PYTHONPATH=tests/_support PYTHONHASHSEED=N python tests/unit/common/test_v07_g6_tester_r3_redaction_oracle.py SEED SECONDS [long]``.
"""

from __future__ import annotations

import os
import sys

import pytest
from mf_redaction_oracle import oracle, soak

pytestmark = pytest.mark.unit


def test_oracle_matches_documented_examples() -> None:
    assert oracle({"A": b"abcd"}, b"x abcd y", preserve=False) == b"x [secret:A] y"
    assert (
        oracle({"A": b"abcd"}, b"abcdabcd", preserve=False) == b"[secret:A][secret:A]"
    )
    assert oracle({"A": b"abcd"}, b"xabcdx", preserve=True) == b"x****x"


def test_short_soak_against_the_oracle() -> None:
    st = soak(int(os.environ.get("PYTHONHASHSEED", "0")) + 1000, 8)
    assert not st.bad, st.line() + "\n" + "\n".join(st.bad[:3])
    assert st.matchers > 50


if __name__ == "__main__":
    seed, secs = int(sys.argv[1]), float(sys.argv[2])
    s = soak(seed, secs, long=len(sys.argv) > 3 and sys.argv[3] == "long")
    print(
        f"PYTHONHASHSEED={os.environ.get('PYTHONHASHSEED')} seed={seed} {s.line()}",
        flush=True,
    )
    for b in s.bad[:5]:
        print(b, flush=True)
    sys.exit(1 if s.bad else 0)
