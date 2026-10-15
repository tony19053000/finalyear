/**
 * Typed errors of the dashboard's API client (D171).
 *
 * Every controller failure is the envelope `{"error": {code, message, reason?}}`
 * (ARCHITECTURE §20); `code` is a FailureType. The client turns it into an
 * `ApiError`; a request that never got an HTTP answer is a `NetworkError`.
 */
import type { components } from "./schema";

export type FailureType = components["schemas"]["FailureType"];
export type ErrorDetail = components["schemas"]["ErrorDetail"];

export class ApiError extends Error {
  override readonly name = "ApiError";
  readonly status: number;
  /** The FailureType, or "http_<status>" when the body was not an envelope. */
  readonly code: string;
  readonly reason: string | undefined;
  /** Seconds from a `Retry-After` header (429 / 503), when present. */
  readonly retryAfterS: number | undefined;
  readonly detail: Readonly<Record<string, unknown>>;

  constructor(
    status: number,
    code: string,
    message: string,
    options: { reason?: string | undefined; retryAfterS?: number | undefined; detail?: Record<string, unknown> } = {},
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.reason = options.reason;
    this.retryAfterS = options.retryAfterS;
    this.detail = options.detail ?? {};
  }
}

/** No HTTP answer at all: controller down, network gone, TLS refused. */
export class NetworkError extends Error {
  override readonly name = "NetworkError";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseRetryAfter(header: string | null): number | undefined {
  if (header === null) return undefined;
  const seconds = Number(header);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined;
}

/** Build the ApiError for a non-2xx response whose body was already read. */
export function errorFromResponse(status: number, headers: Headers, body: unknown): ApiError {
  const retryAfterS = parseRetryAfter(headers.get("retry-after"));
  const error = isRecord(body) ? body.error : undefined;
  if (isRecord(error) && typeof error.code === "string" && typeof error.message === "string") {
    const reason = typeof error.reason === "string" ? error.reason : undefined;
    return new ApiError(status, error.code, error.message, { reason, retryAfterS, detail: error });
  }
  return new ApiError(status, `http_${status}`, `The controller answered HTTP ${status}.`, { retryAfterS });
}

/** A one-line, user-facing message and the machine code of any error. */
export function describeError(error: unknown): { message: string; code?: string } {
  if (error instanceof ApiError) {
    return { message: error.message, code: error.reason ? `${error.code} (${error.reason})` : error.code };
  }
  if (error instanceof NetworkError) {
    return { message: `${error.message} Check that the controller is running and reachable.` };
  }
  if (error instanceof Error) {
    return { message: error.message };
  }
  return { message: "Something went wrong." };
}
