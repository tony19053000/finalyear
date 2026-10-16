/**
 * The dashboard's one HTTP client (D170, D171).
 *
 * - Auth is the HttpOnly `mf_session` cookie (Path=/v1) the browser sends by
 *   itself; this code never sees a token. `credentials: "same-origin"`.
 * - Every mutation (non-GET/HEAD) carries `X-MeshForge-CSRF` = the readable
 *   `mf_csrf` cookie (signed double submit, ARCHITECTURE §16 V0.8). Without
 *   that cookie a mutation is not sent at all: it would only be refused, so
 *   the caller gets a `CsrfMissingError` that says to sign in again (D191 c).
 *   Login and logout are exempt (`csrfOptional`): login has no session yet,
 *   logout must still clear a dead session's cookies.
 * - JSON bodies (`json`) and raw bodies (`body` + `headers`, e.g. blob
 *   uploads, secret values) go through this one function: there is no second
 *   fetch call site in the app (V0.8 G5 folded G4's `rawRequest` in here).
 * - A non-2xx answer becomes a typed `ApiError` from the error envelope; no
 *   answer at all is a `NetworkError`.
 * - A 401 (session ended, revoked, expired) calls the registered handler,
 *   which sends the user to the login page - except for the login call itself.
 */
import { ApiError, CsrfMissingError, NetworkError, errorFromResponse } from "./errors";

export const CSRF_COOKIE = "mf_csrf";
export const CSRF_HEADER = "X-MeshForge-CSRF";

type UnauthorizedHandler = (error: ApiError) => void;

let onUnauthorized: UnauthorizedHandler | null = null;

/** The app shell registers where a 401 sends the user (the login page). */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

// Set while the user's own sign-out is in progress. A read started then would
// come back 401 once the controller closes the session (the browser logs each
// one as a console error), so only the session call itself goes out.
let signingOut = false;

/** Logout: refuse new API calls except `/v1/session` until the next sign-in. */
export function setSigningOut(value: boolean): void {
  signingOut = value;
}

/** Throws when a sign-out is in progress and `path` is not the session call. */
export function assertNotSigningOut(path: string): void {
  if (signingOut && path !== "/v1/session") {
    throw new DOMException("signing out: request not sent", "AbortError");
  }
}

/** The value of cookie `name` readable by script (`mf_csrf` only, by design). */
export function readCookie(name: string, cookies: string = document.cookie): string | undefined {
  for (const part of cookies.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === name) {
      const value = part.slice(eq + 1).trim();
      try {
        return decodeURIComponent(value);
      } catch {
        return value;
      }
    }
  }
  return undefined;
}

export type Method = "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: Method;
  /** JSON-encoded as the request body. */
  json?: unknown;
  /**
   * A raw body (blob upload bytes, a secret value): sent as given. Use
   * `json` or `body`, never both. Set its `Content-Type` in `headers`.
   */
  body?: BodyInit;
  /**
   * Extra request headers (`Idempotency-Key`, the `X-MeshForge-*` part
   * headers). They cannot replace the CSRF header this client sets.
   */
  headers?: Readonly<Record<string, string>>;
  /** Query parameters; undefined values are skipped, arrays repeat the key. */
  query?: Record<string, string | number | boolean | readonly string[] | undefined>;
  signal?: AbortSignal;
  /** Do not call the 401 handler (the login request, the session probe). */
  skipUnauthorizedHandler?: boolean;
  /** Send a mutation even without the `mf_csrf` cookie (login, logout). */
  csrfOptional?: boolean;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  if (!path.startsWith("/v1/") && path !== "/healthz") {
    throw new Error(`API paths start with /v1/ (got ${path})`);
  }
  if (query === undefined) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value as readonly string[]) params.append(key, item);
    } else {
      params.append(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/** Call the controller; resolve with the parsed JSON body (or undefined for 204). */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  assertNotSigningOut(path);
  const method = options.method ?? "GET";
  const url = buildUrl(path, options.query); // a bad path is a bug: throw, not NetworkError
  if (options.json !== undefined && options.body !== undefined) {
    throw new Error("apiRequest: pass `json` or `body`, not both");
  }
  const headers = new Headers({ Accept: "application/json", ...options.headers });
  headers.delete(CSRF_HEADER); // only this client sets it, from the cookie
  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf !== undefined && csrf !== "") {
      headers.set(CSRF_HEADER, csrf);
    } else if (options.csrfOptional !== true) {
      throw new CsrfMissingError();
    }
  }
  let body: BodyInit | undefined = options.body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new NetworkError("The controller did not answer.");
  }

  const text = await response.text();
  let parsed: unknown = undefined;
  if (text !== "") {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
  }

  if (!response.ok) {
    const error = errorFromResponse(response.status, response.headers, parsed);
    if (response.status === 401 && options.skipUnauthorizedHandler !== true) {
      onUnauthorized?.(error);
    }
    throw error;
  }
  if (text !== "" && parsed === undefined) {
    throw new ApiError(response.status, "invalid_response", "The controller sent a body that is not JSON.");
  }
  return parsed as T;
}
