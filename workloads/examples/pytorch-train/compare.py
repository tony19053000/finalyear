# ruff: noqa: T201 - an example program: its output is the point
"""Compare two pytorch-train result folders: exact, else within a tolerance.

    python compare.py outputs/<job_a>/results outputs/<job_b>/results \
        [--rtol 1e-3] [--atol 1e-5]

Exit 0 = identical bytes, or (for runs on different hardware) every weight and
logged loss within ``|a - b| <= atol + rtol * max(|a|, |b|)`` (numpy's isclose
rule: the absolute term keeps near-zero weights from inflating a relative
error); 1 = they differ beyond it. Prints which it was - never "identical" for
a run that only matched within tolerance.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


def _floats(path: Path) -> list[float]:
    data = path.read_bytes()
    return list(struct.unpack(f"<{len(data) // 4}f", data))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    parser.add_argument("--rtol", type=float, default=1e-3)
    parser.add_argument("--atol", type=float, default=1e-5)
    args = parser.parse_args()
    same = all(
        (args.a / name).read_bytes() == (args.b / name).read_bytes()
        for name in ("metrics.json", "weights.bin")
    )
    if same:
        print("IDENTICAL: metrics.json and weights.bin are byte-identical")
        return 0
    wa, wb = _floats(args.a / "weights.bin"), _floats(args.b / "weights.bin")
    ma = json.loads((args.a / "metrics.json").read_text())
    mb = json.loads((args.b / "metrics.json").read_text())
    la = [v for _, v in ma["history"]] + [ma["final_loss"]]
    lb = [v for _, v in mb["history"]] + [mb["final_loss"]]
    if len(wa) != len(wb) or len(la) != len(lb):
        print("DIFFERENT: the runs have different shapes (steps or model)")
        return 1
    # ratio of each difference to its allowance; <= 1 everywhere = within tolerance
    worst = max(
        abs(x - y) / (args.atol + args.rtol * max(abs(x), abs(y)))
        for x, y in zip(wa + la, wb + lb, strict=True)
    )
    largest = max(abs(x - y) for x, y in zip(wa + la, wb + lb, strict=True))
    verdict = "WITHIN TOLERANCE" if worst <= 1 else "DIFFERENT"
    print(
        f"{verdict}: largest absolute difference {largest:.3e} "
        f"(rtol {args.rtol:g}, atol {args.atol:g}; worst {worst:.2f} of the allowance)"
    )
    return 0 if worst <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
