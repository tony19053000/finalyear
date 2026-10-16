"""V0.8 G5 tester r2 (e2e): fix round 1, T-1 edge cases for secrets read from a
file. The PUT body the browser sends is captured from the real request and
compared with the file bytes."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import NORMAL_USER
from mf_dashboard import PageWatch
from playwright.sync_api import Browser, Page, expect

if TYPE_CHECKING:
    from pathlib import Path

    from mf_cluster import Fabric

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]
MAX = 64 * 1024
FILE_LABEL = "Or read the value from a file"


class Puts:
    def __init__(self, page: Page) -> None:
        self.bodies: list[bytes] = []
        self.headers: list[dict[str, str]] = []
        page.on("request", self._on)

    def _on(self, request: object) -> None:
        if getattr(request, "method", "") == "PUT" and "/v1/secrets/" in getattr(
            request, "url", ""
        ):
            self.bodies.append(request.post_data_buffer or b"")  # type: ignore[attr-defined]
            self.headers.append(dict(request.headers))  # type: ignore[attr-defined]


def _open(browser: Browser, fabric: Fabric) -> tuple[Page, PageWatch, Puts, list[str]]:
    page = browser.new_context(reduced_motion="reduce").new_page()
    page.set_default_timeout(20_000)
    watch = PageWatch.attach(page)
    puts = Puts(page)
    console: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    page.goto(fabric.http_base + "/")
    page.get_by_label("User name").fill(NORMAL_USER)
    page.get_by_label("Password").fill(fabric.password(NORMAL_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    return page, watch, puts, console


def _file(tmp_path: Path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_file_secret_edge_cases_are_byte_exact_or_an_honest_refusal(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    page, watch, puts, console = _open(dashboard_browser, fabric)
    d = page.get_by_role("dialog")
    name = d.get_by_label("Name", exact=True)
    chooser = d.get_by_label(FILE_LABEL, exact=False)
    store = d.get_by_role("button", name="Store secret")
    name.fill("EDGE_ONE")

    # empty file and 3 bytes: nothing can be stored, nothing sent
    chooser.set_input_files(_file(tmp_path, "empty", b""))
    expect(store).to_be_disabled()
    chooser.set_input_files(_file(tmp_path, "three", b"abc"))
    expect(store).to_be_disabled()
    assert puts.bodies == []

    # 4 bytes of non-UTF-8: the minimum, exact
    four = bytes([0xFF, 0xFE, 0x00, 0x80])
    chooser.set_input_files(_file(tmp_path, "four", four))
    expect(d).to_contain_text("4 bytes")
    store.click()
    expect(page.get_by_text("Secret EDGE_ONE stored", exact=True)).to_be_visible()
    assert puts.bodies == [four]
    assert puts.headers[-1]["content-type"].startswith("application/octet-stream")

    # exactly 64 KiB of every byte value: accepted, exact
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("EDGE_MAX")
    exact = bytes(i % 256 for i in range(MAX))
    d.get_by_label(FILE_LABEL, exact=False).set_input_files(
        _file(tmp_path, "max", exact)
    )
    d.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret EDGE_MAX stored", exact=True)).to_be_visible()
    assert puts.bodies[-1] == exact and len(puts.bodies[-1]) == MAX

    # 64 KiB + 1: an honest error, nothing sent, and a previously chosen
    # valid file must NOT silently be what gets stored.
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("EDGE_BIG")
    chooser = d.get_by_label(FILE_LABEL, exact=False)
    good = b"GOODFILE-" + os.urandom(16)
    chooser.set_input_files(_file(tmp_path, "good", good))
    chooser.set_input_files(_file(tmp_path, "big", b"x" * (MAX + 1)))
    expect(d).to_contain_text("larger than")
    before = len(puts.bodies)
    store = d.get_by_role("button", name="Store secret")
    if store.is_enabled():
        # the page offers to store while saying the (new) file is too large
        store.click()
        page.wait_for_timeout(1000)
        sent = puts.bodies[before:]
        pytest.fail(
            "after choosing an oversized file the Store button stays enabled "
            f"and would send the PREVIOUS file (sent={[len(b) for b in sent]})"
        )
    assert len(puts.bodies) == before
    page.keyboard.press("Escape")
    watch.assert_clean()
    assert all(b"GOODFILE-" not in line.encode() for line in console)


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_typed_value_after_a_file_replaces_it_and_file_bytes_leave_no_trace(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    page, watch, puts, console = _open(dashboard_browser, fabric)
    d = page.get_by_role("dialog")
    marker = b"FILEMARK-" + os.urandom(8).hex().encode()
    payload = marker + b"\x00\xff\xfe\r\n"
    d.get_by_label("Name", exact=True).fill("mixed")
    d.get_by_label(FILE_LABEL, exact=False).set_input_files(
        _file(tmp_path, "f", payload)
    )
    expect(d).to_contain_text(f"{len(payload)} bytes")
    # nothing of the file is in the DOM (value field, note) or storage
    html = page.content()
    assert marker.decode() not in html
    typed = "typed-after-file-123"
    d.get_by_label("Value", exact=True).fill(typed)
    d.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret MIXED stored", exact=True)).to_be_visible()
    assert puts.bodies == [typed.encode()]  # the file did not leak into the body

    # file chosen AFTER typing replaces the typed text
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("mixed2")
    d.get_by_label("Value", exact=True).fill("will-be-replaced")
    d.get_by_label(FILE_LABEL, exact=False).set_input_files(
        _file(tmp_path, "g", payload)
    )
    expect(d.get_by_label("Value", exact=True)).to_have_value("")
    d.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret MIXED2 stored", exact=True)).to_be_visible()
    assert puts.bodies[-1] == payload

    page.reload()
    state = str(
        page.evaluate(
            "JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage),"
            " location.href, performance.getEntries().map(e => e.name)])"
        )
    )
    assert marker.decode() not in state
    assert marker.decode() not in page.content()
    assert all(marker.decode() not in line for line in console)
    watch.assert_clean()
