import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CSRF_HEADER, apiRequest, readCookie, setUnauthorizedHandler } from "./client";
import { ApiError, CsrfMissingError, NetworkError, describeError, errorFromResponse } from "./errors";
import { shouldRetry } from "./queryClient";

type FetchArgs = [string, RequestInit];

function stubFetch(response: Response | Error) {
  const fn = vi.fn<(...args: FetchArgs) => Promise<Response>>(() =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response.clone()),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

beforeEach(() => {
  setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("readCookie", () => {
  it("finds a cookie by exact name", () => {
    expect(readCookie("mf_csrf", "a=1; mf_csrf=abc%3D; mf_csrfx=no")).toBe("abc=");
    expect(readCookie("mf_csrf", "mf_csrfx=no")).toBeUndefined();
    expect(readCookie("x", "")).toBeUndefined();
  });
});

describe("apiRequest", () => {
  it("GET sends cookies same-origin, no CSRF header, and parses JSON", async () => {
    document.cookie = "mf_csrf=tok123; path=/";
    const fetchMock = stubFetch(json(200, { count: 0, nodes: [] }));
    await expect(apiRequest("/v1/nodes")).resolves.toEqual({ count: 0, nodes: [] });
    const [url, init] = fetchMock.mock.calls[0] ?? ["", {}];
    expect(url).toBe("/v1/nodes");
    expect(init.credentials).toBe("same-origin");
    expect(init.method).toBe("GET");
    expect(new Headers(init.headers).has(CSRF_HEADER)).toBe(false);
  });

  it("mutations carry X-MeshForge-CSRF from the mf_csrf cookie and a JSON body", async () => {
    document.cookie = "mf_csrf=tok123; path=/";
    const fetchMock = stubFetch(json(200, { logged_out: true }));
    await apiRequest("/v1/session", { method: "DELETE" });
    await apiRequest("/v1/jobs", { method: "POST", json: { a: 1 } });
    const [, del] = fetchMock.mock.calls[0] ?? ["", {}];
    expect(new Headers(del.headers).get(CSRF_HEADER)).toBe("tok123");
    const [, post] = fetchMock.mock.calls[1] ?? ["", {}];
    expect(new Headers(post.headers).get("content-type")).toBe("application/json");
    expect(post.body).toBe('{"a":1}');
  });

  it("a mutation without the mf_csrf cookie is not sent: typed error that says to sign in again (D191 c)", async () => {
    document.cookie = "mf_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    expect(readCookie("mf_csrf")).toBeUndefined();
    const fetchMock = stubFetch(json(200, {}));
    const err = await apiRequest("/v1/nodes/n1/revoke", { method: "POST" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(CsrfMissingError);
    expect((err as Error).message).toMatch(/sign in again/i);
    expect(describeError(err).message).toMatch(/sign in again/i);
    expect(fetchMock).not.toHaveBeenCalled();
    // An empty value is no better than none.
    document.cookie = "mf_csrf=; path=/";
    await expect(apiRequest("/v1/jobs", { method: "POST", json: {} })).rejects.toBeInstanceOf(CsrfMissingError);
    // Reads need no CSRF; login and logout are sent without it.
    await expect(apiRequest("/v1/nodes")).resolves.toEqual({});
    await apiRequest("/v1/session", { method: "POST", json: {}, csrfOptional: true });
    await apiRequest("/v1/session", { method: "DELETE", csrfOptional: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [, del] = fetchMock.mock.calls[2] ?? ["", {}];
    expect(new Headers(del.headers).has(CSRF_HEADER)).toBe(false);
  });

  it("builds query strings, repeating array keys and skipping undefined", async () => {
    const fetchMock = stubFetch(json(200, {}));
    await apiRequest("/v1/jobs", { query: { state: ["running", "queued"], limit: 50, owner: undefined } });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/jobs?state=running&state=queued&limit=50");
  });

  it("refuses a path outside /v1", async () => {
    await expect(apiRequest("https://evil.example/v1/x")).rejects.toThrow(/start with \/v1/);
  });

  it("turns the error envelope into a typed ApiError with reason and Retry-After", async () => {
    stubFetch(
      json(429, { error: { code: "resource_busy", message: "slow down", reason: "rate_limited_user" } }, { "retry-after": "30" }),
    );
    const err = await apiRequest("/v1/session", { method: "POST", json: {}, csrfOptional: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect([apiErr.status, apiErr.code, apiErr.reason, apiErr.retryAfterS, apiErr.message]).toEqual([
      429,
      "resource_busy",
      "rate_limited_user",
      30,
      "slow down",
    ]);
  });

  it("a non-envelope error body still yields an ApiError", async () => {
    stubFetch(new Response("<html>bad gateway</html>", { status: 502 }));
    const err = (await apiRequest("/v1/nodes").catch((e: unknown) => e)) as ApiError;
    expect([err.status, err.code]).toEqual([502, "http_502"]);
  });

  it("no answer at all is a NetworkError", async () => {
    stubFetch(new TypeError("Failed to fetch"));
    await expect(apiRequest("/v1/nodes")).rejects.toBeInstanceOf(NetworkError);
  });

  it("a 2xx body that is not JSON is an error, not silent garbage", async () => {
    stubFetch(new Response("<!doctype html>", { status: 200 }));
    await expect(apiRequest("/v1/nodes")).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("401 calls the unauthorized handler, unless the call opted out", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    stubFetch(json(401, { error: { code: "unauthorized", message: "the session has ended" } }));
    await expect(apiRequest("/v1/nodes")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    stubFetch(json(401, { error: { code: "unauthorized", message: "bad password" } }));
    await expect(apiRequest("/v1/session", { method: "POST", skipUnauthorizedHandler: true, csrfOptional: true })).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe("errors", () => {
  it("errorFromResponse ignores a malformed retry-after", () => {
    const err = errorFromResponse(503, new Headers({ "retry-after": "soon" }), {
      error: { code: "network_error", message: "x" },
    });
    expect(err.retryAfterS).toBeUndefined();
  });

  it("describeError covers every kind", () => {
    expect(describeError(new ApiError(403, "unauthorized", "no", { reason: "csrf" }))).toEqual({
      message: "no",
      code: "unauthorized (csrf)",
    });
    expect(describeError(new Error("boom"))).toEqual({ message: "boom" });
    expect(describeError("weird")).toEqual({ message: "Something went wrong." });
  });

  it("retries transient failures only", () => {
    expect(shouldRetry(0, new ApiError(503, "x", "y"))).toBe(true);
    expect(shouldRetry(0, new ApiError(429, "x", "y"))).toBe(true);
    expect(shouldRetry(0, new ApiError(404, "not_found", "y"))).toBe(false);
    expect(shouldRetry(0, new NetworkError("down"))).toBe(true);
    expect(shouldRetry(2, new NetworkError("down"))).toBe(false);
  });
});
