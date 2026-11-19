import { afterEach, describe, expect, it, vi } from "vitest";

import { demoSessionRoute } from "./install";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("demo admin session is development-only", () => {
  it("the vite dev server (DEV) answers /v1/session and /v1/auth/whoami with the demo admin", () => {
    vi.stubEnv("DEV", true);
    expect(demoSessionRoute("/v1/session")).toMatchObject({ session: { user: { role: "admin" } } });
    expect(demoSessionRoute("/v1/auth/whoami")).toMatchObject({ user: { role: "admin" } });
  });

  it("a production build (DEV false) never answers the session routes", () => {
    vi.stubEnv("DEV", false);
    expect(demoSessionRoute("/v1/session")).toBeUndefined();
    expect(demoSessionRoute("/v1/auth/whoami")).toBeUndefined();
  });

  it("other paths are never session answers", () => {
    vi.stubEnv("DEV", true);
    expect(demoSessionRoute("/v1/nodes")).toBeUndefined();
  });
});
