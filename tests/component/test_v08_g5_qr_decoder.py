"""V0.8 G5 (D199): the test-side QR decoder (``tests/_support/qr_decode.py``)
reads back what the dashboard's QR encoder (npm ``qrcode-generator``, through
``frontend/src/components/qr.ts``'s settings: byte mode, ECC M) draws - for
real join codes (IPv4 and long host-name forms) and across versions - and a
single flipped module is caught by its Reed-Solomon check, never mis-read."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from qr_decode import QrDecodeError, decode_matrix, matrix_from_svg_path

from meshforge.common.join_code import JoinCode, encode

pytestmark = pytest.mark.component

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
# The same calls as frontend/src/components/qr.ts (qrModules).
SCRIPT = """
import qrcode from "qrcode-generator";
const texts = JSON.parse(process.argv[1]);
const out = texts.map((text) => {
  const qr = qrcode(0, "M");
  qr.addData(text, "Byte");
  qr.make();
  const n = qr.getModuleCount();
  return Array.from({ length: n }, (_, r) => Array.from({ length: n }, (_, c) => qr.isDark(r, c)));
});
process.stdout.write(JSON.stringify(out));
"""


def _encode(texts: list[str]) -> list[list[list[bool]]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not (FRONTEND / "node_modules" / "qrcode-generator").is_dir():
        pytest.skip("frontend/node_modules is missing - run `cd frontend && npm ci`")
    done = subprocess.run(
        [node, "--input-type=module", "-e", SCRIPT, json.dumps(texts)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout))


def _codes() -> list[str]:
    ipv4 = encode(
        JoinCode(host="10.0.0.1", port=47101, token=b"\x01" * 16, ca_pin="02" * 16)
    )
    long_host = encode(
        JoinCode(
            host="controller." + ".".join(["x" * 60] * 3) + ".lab.example",
            port=443,
            token=b"\xff" * 16,
            ca_pin="00" * 16,
        )
    )
    return [ipv4, long_host]


def test_join_codes_and_other_lengths_round_trip() -> None:
    texts = [
        *_codes(),
        "A",
        "mf1-" + "a" * 60,
        "x" * 300,
        "~!@#$%^&*()_+{}|:<>?` -=[];',./",
    ]
    matrices = _encode(texts)
    versions = set()
    for text, matrix in zip(texts, matrices, strict=True):
        versions.add((len(matrix) - 17) // 4)
        assert decode_matrix(matrix) == text
    assert len(versions) >= 4, versions  # small, middle (>= 7: version info) and large


def test_one_flipped_data_module_is_refused_not_misread() -> None:
    text = _codes()[0]
    matrix = _encode([text])[0]
    size = len(matrix)
    # A data module far from every function pattern (bottom-right region).
    matrix[size - 3][size - 3] = not matrix[size - 3][size - 3]
    with pytest.raises(QrDecodeError, match="Reed-Solomon"):
        decode_matrix(matrix)


def test_the_svg_path_form_reads_back() -> None:
    text = _codes()[0]
    matrix = _encode([text])[0]
    margin = 4
    path = "".join(
        f"M{x + margin} {y + margin}h1v1h-1z"
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
    )
    assert matrix_from_svg_path(path, len(matrix), margin) == matrix
    assert decode_matrix(matrix_from_svg_path(path, len(matrix), margin)) == text
