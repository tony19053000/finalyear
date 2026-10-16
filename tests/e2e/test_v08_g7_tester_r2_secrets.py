"""V0.8 G7 tester r2: the T-9 fix (a secret file the browser cannot read) in a
real Chromium against a real controller - sequences of good, broken, slow and
oversized files, and what is (not) sent."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
from mf_e2e import sign_in
from playwright.sync_api import expect

if TYPE_CHECKING:
    from pathlib import Path

    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Locator, Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
    pytest.mark.parametrize("fabric_options", [{"agents": 1}]),
]

HOOK = """
(() => {
  const real = File.prototype.arrayBuffer;
  File.prototype.arrayBuffer = function () {
    if (this.name.startsWith('slow')) {
      return new Promise((ok) => setTimeout(() => ok(real.call(this)), 1200));
    }
    if (this.name.startsWith('broken')) {
      return Promise.reject(new DOMException('read failed', 'NotReadableError'));
    }
    return real.call(this);
  };
})();
"""


def _form(page: Page, fabric: Fabric, name: str) -> tuple[Locator, Locator]:
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill(name)
    expect(dialog).to_be_visible()
    return dialog, dialog.get_by_label("Or read the value from a file", exact=False)


def _puts(page: Page) -> list[bytes | None]:
    sent: list[bytes | None] = []
    page.on(
        "request",
        lambda r: sent.append(r.post_data_buffer) if r.method == "PUT" else None,
    )
    return sent


def test_good_then_broken_sends_nothing_and_a_later_good_file_recovers(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page(init=HOOK)
    sign_in(page, fabric)
    sent = _puts(page)
    dialog, chooser = _form(page, fabric, "r2_one")
    (tmp_path / "good1.bin").write_bytes(b"GOOD-ONE-VALUE")
    (tmp_path / "broken.bin").write_bytes(b"x")
    (tmp_path / "good2.bin").write_bytes(b"GOOD\x00TWO\xff\n")
    chooser.set_input_files(tmp_path / "good1.bin")
    expect(dialog).to_contain_text("good1.bin")
    chooser.set_input_files(tmp_path / "broken.bin")
    expect(dialog.get_by_role("alert")).to_contain_text("Could not read broken.bin")
    expect(dialog).not_to_contain_text("good1.bin")
    store = dialog.get_by_role("button", name="Store secret")
    expect(store).to_be_disabled()
    store.click(force=True)  # a disabled button sends nothing
    dialog.get_by_label("Name", exact=True).press("Enter")  # nor does Enter
    page.wait_for_timeout(500)
    assert sent == []
    chooser.set_input_files(tmp_path / "good2.bin")
    expect(dialog.get_by_role("alert")).to_have_count(0)
    with page.expect_request(lambda r: r.method == "PUT") as req:
        store.click()
    assert req.value.post_data_buffer == b"GOOD\x00TWO\xff\n"
    assert watch.page_errors == []


def test_a_slow_read_finishing_after_a_newer_broken_file_does_not_come_back(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page(init=HOOK)
    sign_in(page, fabric)
    sent = _puts(page)
    dialog, chooser = _form(page, fabric, "r2_two")
    (tmp_path / "slow.bin").write_bytes(b"SLOW-VALUE-123")
    (tmp_path / "broken.bin").write_bytes(b"x")
    chooser.set_input_files(tmp_path / "slow.bin")
    chooser.set_input_files(tmp_path / "broken.bin")
    expect(dialog.get_by_role("alert")).to_contain_text("broken.bin")
    page.wait_for_timeout(2000)  # the slow read has now finished
    expect(dialog.get_by_role("alert")).to_contain_text("broken.bin")
    expect(dialog).not_to_contain_text("slow.bin")
    expect(dialog.get_by_role("button", name="Store secret")).to_be_disabled()
    assert sent == [] and watch.page_errors == []


def test_a_broken_read_finishing_after_a_newer_good_file_does_not_show_an_alert(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    hook = HOOK.replace(
        "if (this.name.startsWith('broken')) {",
        "if (this.name.startsWith('broken')) { return new Promise((_, no) => "
        "setTimeout(() => no(new DOMException('late', 'NotReadableError')), 1200)); }"
        " if (false) {",
    )
    page, watch = new_page(init=hook)
    sign_in(page, fabric)
    dialog, chooser = _form(page, fabric, "r2_three")
    (tmp_path / "broken.bin").write_bytes(b"x")
    (tmp_path / "ok.bin").write_bytes(b"OK-VALUE-1234")
    chooser.set_input_files(tmp_path / "broken.bin")
    chooser.set_input_files(tmp_path / "ok.bin")
    expect(dialog).to_contain_text("ok.bin")
    page.wait_for_timeout(2000)
    expect(dialog.get_by_role("alert")).to_have_count(0)
    expect(dialog).to_contain_text("ok.bin")
    with page.expect_request(lambda r: r.method == "PUT") as req:
        dialog.get_by_role("button", name="Store secret").click()
    assert req.value.post_data_buffer == b"OK-VALUE-1234"
    assert watch.page_errors == []


def test_a_broken_file_then_an_oversized_one_replaces_the_alert_by_the_size_note(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page(init=HOOK)
    sign_in(page, fabric)
    sent = _puts(page)
    dialog, chooser = _form(page, fabric, "r2_four")
    (tmp_path / "broken.bin").write_bytes(b"x")
    (tmp_path / "big.bin").write_bytes(b"A" * (64 * 1024 + 1))
    chooser.set_input_files(tmp_path / "broken.bin")
    expect(dialog.get_by_role("alert")).to_contain_text("broken.bin")
    chooser.set_input_files(tmp_path / "big.bin")
    expect(dialog.get_by_role("alert")).to_have_count(0)
    expect(dialog).to_contain_text("larger than 64 KiB")
    expect(dialog.get_by_role("button", name="Store secret")).to_be_disabled()
    assert sent == [] and watch.page_errors == []


def test_a_broken_file_name_with_markup_is_shown_as_text(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page(init=HOOK)
    sign_in(page, fabric)
    dialog, chooser = _form(page, fabric, "r2_five")
    name = "broken<img src=x onerror=window.__xss=1>.bin"
    (tmp_path / name).write_bytes(b"x")
    chooser.set_input_files(tmp_path / name)
    expect(dialog.get_by_role("alert")).to_contain_text("<img src=x onerror=")
    assert page.evaluate("window.__xss") is None
    assert page.locator("dialog img, [role=dialog] img").count() == 0
    assert watch.page_errors == []


def test_closing_and_reopening_the_dialog_forgets_the_broken_file_alert(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page(init=HOOK)
    sign_in(page, fabric)
    dialog, chooser = _form(page, fabric, "r2_six")
    (tmp_path / "broken.bin").write_bytes(b"x")
    chooser.set_input_files(tmp_path / "broken.bin")
    expect(dialog.get_by_role("alert")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    page.get_by_role("button", name="Set a secret").click()
    expect(page.get_by_role("dialog").get_by_role("alert")).to_have_count(0)
    assert watch.page_errors == []
