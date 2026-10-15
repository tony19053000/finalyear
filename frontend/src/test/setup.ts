// Vitest setup: unmount React trees after every test; fill jsdom gaps.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  document.cookie.split(";").forEach((part) => {
    const name = part.split("=")[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
});

// jsdom has HTMLDialogElement but not its modal methods (Dialog.tsx uses
// them). Minimal stand-ins: the open attribute and the close event - the
// real top-layer/inert behaviour is covered by the Playwright e2e tests.
const proto = HTMLDialogElement.prototype;
if (typeof proto.showModal !== "function") {
  proto.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
}
if (typeof proto.close !== "function") {
  proto.close = function close(this: HTMLDialogElement) {
    if (!this.hasAttribute("open")) return;
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}
