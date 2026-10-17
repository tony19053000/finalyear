"""V0.9 G0 item 5 (D210 item 3): on the Secrets page the SAME file can be
chosen again - after typing over it, after a refused store, and in a new
form after a stored one - in real Chromium against a real controller.

Chromium fires ``change`` on a file input only when its file list changes:
choosing the file the input already holds does nothing at all (checked with
a bare page below), so these tests fail if the page stops resetting the
input after each choice.
"""

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
    from playwright.sync_api import Locator, Page, Route

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
    pytest.mark.parametrize("fabric_options", [{"agents": 1}]),
]

VALUE = b"REPICK-FILE-VALUE-\x00\xff-1"


def _form(page: Page, fabric: Fabric, name: str) -> tuple[Locator, Locator]:
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Name", exact=True).fill(name)
    return dialog, dialog.get_by_label("Or read the value from a file", exact=False)


def _note(dialog: Locator) -> Locator:
    return dialog.get_by_text("Value read from same.bin")


def test_chromium_ignores_choosing_the_file_an_input_already_holds(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    """The premise: without a reset, re-choosing the same file is no event."""
    page, _watch = new_page()
    page.goto(fabric.http_base + "/")  # any page of the app's origin
    page.evaluate(
        """() => {
          const i = document.createElement('input');
          i.type = 'file'; i.id = 'bare'; window.__n = 0;
          i.setAttribute('aria-label', 'probe file input');
          i.addEventListener('change', () => { window.__n += 1; });
          document.body.append(i);
        }"""
    )
    same = tmp_path / "same.bin"
    same.write_bytes(VALUE)
    for _ in range(3):
        page.locator("#bare").set_input_files(same)
    assert page.evaluate("window.__n") == 1


def test_the_same_file_can_be_chosen_again_after_typing_over_it(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page()
    sign_in(page, fabric)
    dialog, chooser = _form(page, fabric, "repick_one")
    same = tmp_path / "same.bin"
    same.write_bytes(VALUE)
    chooser.set_input_files(same)
    expect(_note(dialog)).to_be_visible()
    assert chooser.evaluate("(i) => [i.value, i.files.length]") == ["", 0]
    dialog.get_by_label("Value", exact=True).fill("TYPED-OVER-IT")
    expect(_note(dialog)).to_have_count(0)
    chooser.set_input_files(same)  # the same file again
    expect(_note(dialog)).to_be_visible()
    with page.expect_request(lambda r: r.method == "PUT") as req:
        dialog.get_by_role("button", name="Store secret").click()
    assert req.value.post_data_buffer == VALUE
    expect(page.get_by_text("Secret REPICK_ONE stored")).to_be_visible()
    assert watch.page_errors == []


def test_the_same_file_can_be_chosen_again_after_a_refused_store(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page()
    watch.allow("status of 503", "/v1/secrets/REPICK_TWO", "the refused first store")
    sign_in(page, fabric)
    refused: list[str] = []

    def once(route: Route) -> None:
        if route.request.method == "PUT" and not refused:
            refused.append(route.request.url)
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"error":{"code":"resource_busy","message":"Busy, try again."}}',
            )
        else:
            route.continue_()

    page.route("**/v1/secrets/REPICK_TWO", once)
    dialog, chooser = _form(page, fabric, "repick_two")
    same = tmp_path / "same.bin"
    same.write_bytes(VALUE)
    chooser.set_input_files(same)
    expect(_note(dialog)).to_be_visible()
    dialog.get_by_role("button", name="Store secret").click()
    expect(dialog.get_by_role("alert")).to_contain_text("Not stored")
    expect(_note(dialog)).to_have_count(0)  # the value was cleared
    chooser.set_input_files(same)  # pick the same file to retry
    expect(_note(dialog)).to_be_visible()
    with page.expect_request(lambda r: r.method == "PUT") as req:
        dialog.get_by_role("button", name="Store secret").click()
    assert req.value.post_data_buffer == VALUE
    expect(page.get_by_text("Secret REPICK_TWO stored")).to_be_visible()
    assert len(refused) == 1 and watch.page_errors == []


def test_the_same_file_works_in_a_new_form_after_a_stored_one(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page()
    sign_in(page, fabric)
    same = tmp_path / "same.bin"
    same.write_bytes(VALUE)
    for name in ("repick_three", "repick_four"):
        dialog, chooser = _form(page, fabric, name)
        chooser.set_input_files(same)
        expect(_note(dialog)).to_be_visible()
        with page.expect_request(lambda r: r.method == "PUT") as req:
            dialog.get_by_role("button", name="Store secret").click()
        assert req.value.post_data_buffer == VALUE
        expect(page.get_by_text(f"Secret {name.upper()} stored")).to_be_visible()
    assert watch.page_errors == []
