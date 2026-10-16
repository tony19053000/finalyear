// V0.8 G3+G4 merge (D198): while the user's own sign-out is in progress no new
// API call goes out except the session call, so nothing comes back 401.
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest, setSigningOut } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("signing-out gate", () => {
  it("refuses reads without touching the network, lets the session call through, reopens on demand", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } })));
    vi.stubGlobal("fetch", fetchMock);
    setSigningOut(true);
    await expect(apiRequest("/v1/nodes")).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).not.toHaveBeenCalled();
    await apiRequest("/v1/session");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    setSigningOut(false);
    await apiRequest("/v1/nodes");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
