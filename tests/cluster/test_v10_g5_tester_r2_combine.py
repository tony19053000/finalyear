"""V0.10 G5 tester r2 (REAL controller + agent processes).

* T2: one tree and one item list run as jobs with several chunk sizes in
  sequence on ONE controller (6, 30 = a single chunk, 4 = ragged last chunk, 6
  again): every job succeeds with the identical combined output;
* N6: a combine script that forges the built-in's ``result.json`` and exits
  with the built-in's refusal code is still ``script_failed``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric

sys.path.insert(0, str(Path(__file__).parent))
import test_v10_g5_tester_r1_combine as r1

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

FORGER = """\
import json, os, sys
out = os.environ["MESHFORGE_COMBINE_OUT"]
root = os.path.dirname(out.rstrip("/\\\\"))
for path in (os.path.join(root, "result.json"), os.path.join(out, "result.json")):
    try:
        with open(path, "w") as fh:
            json.dump({"ok": False, "code": "header_mismatch", "message": "FORGED"}, fh)
    except OSError:
        pass
sys.exit(2)
"""


def test_item_list_with_a_sequence_of_chunk_sizes(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = r1._fabric(make_fabric)
    r1._project(tmp_path / "p")
    tree = r1._upload(fabric, tmp_path / "p")
    outputs: list[str] = []
    for n, size in enumerate((6, r1.ITEMS, 4, 6)):
        job = r1._wait(fabric, r1._submit(fabric, tree, chunk=size), 240)
        assert job["state"] == "succeeded", (
            size,
            job["failure_type"],
            job["failure_detail"],
        )
        lanes = [a for a in job["attempts"] if a.get("role") == "lane"]
        assert lanes, size
        assert len(r1._combines(job)) == 1
        got = r1._output(fabric, job, tmp_path / f"o{n}")
        assert (got / "labels.csv").read_bytes() == r1._expected_csv(), size
        outputs.append(job["output_digest"])
    assert len(set(outputs)) == 1


def test_script_combine_cannot_forge_the_builtin_result(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = r1._fabric(make_fabric)
    root = tmp_path / "p"
    r1._project(root)
    (root / "combine.py").write_text(FORGER)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    job = r1._wait(
        fabric, r1._submit(fabric, r1._upload(fabric, root), combine=script), 240
    )
    assert job["state"] == "failed", job
    assert job["failure_type"] == "combine_error"
    detail = job["failure_detail"] or ""
    assert detail.startswith("script_failed"), detail
    assert "FORGED" not in detail and "header_mismatch" not in detail
    assert len(r1._combines(job)) == 1  # not retried
