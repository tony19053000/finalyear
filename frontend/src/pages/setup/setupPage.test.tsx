/**
 * V0.17 G2 (D392): the "create your fabric" page against a fake controller.
 * The token leaves the address bar at once, is sent only to /v1/setup/session,
 * the password rule is pre-checked, the join code is shown with its QR code,
 * and an already set-up controller (410) gets the sign-in link.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fakeController, json } from "../../test/fakeFabric";
import { SetupPage } from "./SetupPage";
import { passwordProblem, tokenFromHash } from "./setupApi";

const TOKEN = "Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5";
const CODE = "mf1-aebag-aaaab-6c3ab-zz2qa-qaaaa-aaaaa-aaaaa-aaaab-aaaaa-aaaaa-aaaaa-aaaaa-7777a";
const PASSWORD = "correct horse battery staple";

const INFO = {
  state: "pending",
  hostname: "lab-pc",
  enroll_address: "192.168.1.10:47101",
  address_problem: null,
  dashboard_url: "https://192.168.1.10:47110/",
  loopback_only: false,
  mdns: true,
  config_file: "/etc/meshforge/controller.toml",
  checks: {
    ok: true,
    checks: [
      { name: "postgresql binaries", status: "ok", detail: "PostgreSQL 16.15", hint: "" },
      { name: "fabric CA", status: "warn", detail: "not created yet", hint: "create the fabric" },
    ],
  },
};

const RESULT = {
  admin: "asha",
  fabric_id: "fab_01ABC",
  ca_fingerprint: "ab".repeat(32),
  join_fingerprint: "abcd-efgh",
  invite: {
    code: CODE,
    invite_id: "inv_1",
    address: "192.168.1.10:47101",
    ca_pin: "ab",
    expires_at: "2026-10-25T10:15:00+00:00",
  },
  invite_problem: null,
  dashboard_url: "https://192.168.1.10:47110/",
  restarting: true,
};

function renderPage(hash: string) {
  window.history.replaceState(null, "", `/setup${hash}`);
  return render(
    <MemoryRouter>
      <SetupPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("setup helpers", () => {
  it("reads the token from the fragment only", () => {
    expect(tokenFromHash(`#token=${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromHash(`#t=${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromHash("")).toBeNull();
    expect(tokenFromHash("#token=")).toBeNull();
  });

  it("pre-checks the admin password rule", () => {
    expect(passwordProblem("asha", "short", "short")).toMatch(/12/);
    expect(passwordProblem("asha", "aaaaaaaaaaaaaa", "aaaaaaaaaaaaaa")).toMatch(/different/);
    expect(passwordProblem("asha", "asha-is-admin-2026", "asha-is-admin-2026")).toMatch(/user name/);
    expect(passwordProblem("asha", PASSWORD, "other")).toMatch(/same/);
    expect(passwordProblem("asha", PASSWORD, PASSWORD)).toBeNull();
  });
});

describe("SetupPage", () => {
  it("swaps the token for a session, creates the fabric and shows the join code", async () => {
    const fake = fakeController({
      "POST /v1/setup/session": () => json(200, { state: "pending", session: true }),
      "GET /v1/setup/info": () => json(200, INFO),
      "POST /v1/setup/complete": () => json(200, RESULT),
    });
    renderPage(`#token=${TOKEN}`);
    expect(window.location.hash).toBe(""); // gone from the address bar at once
    await screen.findByText("lab-pc");
    expect(screen.getByText("192.168.1.10:47101")).toBeTruthy();
    const session = fake.calls.find((c) => c.key === "POST /v1/setup/session");
    expect(session?.init.body).toBe(JSON.stringify({ token: TOKEN }));
    expect(fake.calls.every((c) => !c.url.includes(TOKEN))).toBe(true);

    const user = userEvent.setup();
    const name = screen.getByLabelText("Admin user name");
    await user.clear(name);
    await user.type(name, "asha");
    await user.type(screen.getByLabelText("Password"), "short");
    expect(screen.getByRole("alert").textContent).toMatch(/12/);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Create the fabric" }).disabled).toBe(true);
    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), PASSWORD);
    await user.type(screen.getByLabelText("Repeat the password"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create the fabric" }));

    await screen.findByText("Your fabric is ready");
    expect(screen.getByText(CODE)).toBeTruthy();
    expect(screen.getByRole("img", { name: "QR code of the join code" })).toBeTruthy();
    expect(screen.getByRole("link", { name: RESULT.dashboard_url })).toBeTruthy();
    const complete = fake.calls.find((c) => c.key === "POST /v1/setup/complete");
    expect(complete?.init.body).toBe(JSON.stringify({ admin: "asha", password: PASSWORD }));
    expect(document.body.textContent).not.toContain(PASSWORD);
  });

  it("asks for the setup link when there is no token and no session", async () => {
    fakeController({ "GET /v1/setup/state": () => json(200, { state: "pending", session: false }) });
    renderPage("");
    await screen.findByText("Open your setup link");
    expect(screen.getByText("sudo meshforge-controller setup url")).toBeTruthy();
  });

  it("says a wrong token is not valid", async () => {
    fakeController({
      "POST /v1/setup/session": () =>
        json(401, { error: { code: "unauthorized", message: "this setup link is not valid" } }),
    });
    renderPage("#token=wrong");
    await screen.findByText("this setup link is not valid");
  });

  it("sends an already set-up controller's visitor to sign in (410)", async () => {
    fakeController({
      "GET /v1/setup/state": () => json(410, { error: { code: "conflict", message: "already set up" } }),
    });
    renderPage("");
    await screen.findByText("This controller is already set up");
    expect(screen.getByRole("link", { name: "Go to the dashboard" }).getAttribute("href")).toBe("/");
  });

  it("shows the server's refusal of a password", async () => {
    fakeController({
      "POST /v1/setup/session": () => json(200, { state: "pending", session: true }),
      "GET /v1/setup/info": () => json(200, { ...INFO, loopback_only: true }),
      "POST /v1/setup/complete": () =>
        json(400, { error: { code: "invalid_input", message: "this password is too common; choose another" } }),
    });
    renderPage(`#token=${TOKEN}`);
    await screen.findByText(/listens on this computer only/);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Password"), PASSWORD);
    await user.type(screen.getByLabelText("Repeat the password"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create the fabric" }));
    await screen.findByText("this password is too common; choose another");
  });
});
