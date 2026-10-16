import { ApiError, NetworkError } from "../api/errors";

/** Router state the login page understands (never in the URL). */
export interface LoginState {
  from?: string;
  reason?: "expired" | "logged_out";
}

/** A user-facing message for a refused login: what happened and what to do. */
export function loginErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong user name or password.";
    if (error.status === 429) {
      const wait =
        error.retryAfterS !== undefined ? ` Try again in ${Math.ceil(error.retryAfterS)} s.` : " Try again later.";
      return `Too many login attempts.${wait}`;
    }
    if (error.status === 403) {
      return "This login was refused because it did not come from the dashboard page itself. Reload the page and try again.";
    }
    if (error.status === 503) return "The controller's user database is unavailable. Try again in a moment.";
    return `${error.message} (${error.code})`;
  }
  if (error instanceof NetworkError) return "The controller did not answer. Check that it is running and reachable.";
  return "Login failed.";
}
