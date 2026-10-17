"""V0.8 G7 tester r2 (T-1): the leak verdict with a REAL page leaking.

Fix round 1's own test plants a string straight into ``leak_watch.places``.
Here a throw-away test file opens real Chromium pages against a real
controller, stores a registered secret (awkward characters) and a ``mfat_``
token shape in the browser, and a real pytest subprocess must exit non-zero,
name the place, and never print any form of the value - whatever is selected,
in whatever order, even when the leaking test is first and a later test never
opens a page, and even when the leak was removed again before the test ended.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
    pytest.mark.timeout(900),
]

ROOT = Path(__file__).resolve().parents[2]
E2E = Path(__file__).resolve().parent
TOKEN = "mfat_" + "R2tokenAAAAAAAAAAAA"
SECRET = 'r2-"plant\\ed"-secret-' + "Zk4" * 4

PLANTED = """
import pytest

pytestmark = pytest.mark.e2e
SECRET = %(secret)r
TOKEN = %(token)r


def test_a_first_leaks_then_cleans_up(fabric, new_page, leak_watch) -> None:
    leak_watch.add_known(SECRET)
    page, _ = new_page()
    page.goto(fabric.http_base + "/login")
    page.evaluate(
        "([s, t]) => { sessionStorage.setItem('k', s); sessionStorage.setItem('t', t);"
        " sessionStorage.clear(); }",
        [SECRET, TOKEN],
    )


def test_b_clean_and_without_a_page() -> None:
    assert 1 + 1 == 2


def test_c_clean_with_a_page(fabric, new_page) -> None:
    page, _ = new_page()
    page.goto(fabric.http_base + "/login")
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=800,
        env=env,
        check=False,
    )


@pytest.fixture
def planted() -> Iterator[Path]:
    path = E2E / f"test_zzzz_tmp_r2_real_leak_{secrets.token_hex(4)}.py"
    path.write_text(PLANTED % {"secret": SECRET, "token": TOKEN}, "utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _forms() -> list[str]:
    raw = SECRET.encode()
    return [
        SECRET,
        json.dumps(SECRET)[1:-1],
        json.dumps(json.dumps(SECRET))[1:-1],
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
        TOKEN,
        TOKEN[5:],
    ]


@pytest.mark.parametrize(
    "selection",
    [
        [],  # the file alone, file order
        ["-k", "test_c or test_a"],  # a subset
        ["-k", "not test_a_first"],
    ],
    ids=["alone", "subset", "leaker-deselected-is-clean"],
)
def test_t1_a_real_page_leak_fails_the_run_and_prints_no_value(
    planted: Path, selection: list[str]
) -> None:
    rel = str(planted.relative_to(ROOT))
    done = _run(rel, *selection)
    output = done.stdout + done.stderr
    if "not test_a_first" in selection:
        assert done.returncode == 0, output[-3000:]
        assert "credentials reached the browser" not in output
        return
    assert done.returncode != 0, output[-3000:]
    assert "credentials reached the browser" in output, output[-3000:]
    assert "holds a known credential" in output and "mfat... shape" in output
    for form in _forms():
        assert form not in output, f"the value leaked into the report: {form[:6]}..."


def test_t1_the_leaker_running_first_or_last_gives_the_same_verdict(
    planted: Path,
) -> None:
    rel = str(planted.relative_to(ROOT))
    leaker = f"{rel}::test_a_first_leaks_then_cleans_up"
    clean = f"{rel}::test_c_clean_with_a_page"
    for order in ([leaker, clean], [clean, leaker]):
        done = _run("-p", "no:randomly", *order)
        assert done.returncode != 0, (order, (done.stdout + done.stderr)[-2000:])
        assert "credentials reached the browser" in done.stdout + done.stderr
