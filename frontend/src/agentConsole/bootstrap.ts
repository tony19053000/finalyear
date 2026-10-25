/**
 * The launcher opens `http://127.0.0.1:47130/#c=<one-time code>` (never the
 * token itself: that only buys launch codes). The page trades it for the
 * HttpOnly session cookie once and removes it from the address bar and the
 * history at once - before the request, so a failed exchange leaves nothing
 * behind either. The fragment never reaches a server log (browsers do not send
 * it), and nothing is kept in browser storage.
 */
export interface LaunchCredential {
  code: string;
}

const MAX_LENGTH = 256;
const SAFE = /^[A-Za-z0-9_-]+$/;

/** The launch code in `hash` (`#c=...`), or null. */
export function readCredential(hash: string): LaunchCredential | null {
  const text = hash.startsWith("#") ? hash.slice(1) : hash;
  if (text === "") return null;
  const params = new URLSearchParams(text);
  const code = params.get("c");
  if (code !== null && code.length <= MAX_LENGTH && SAFE.test(code)) return { code };
  return null;
}

/** Drop the fragment from the address bar without a new history entry. */
export function clearFragment(win: Pick<Window, "history" | "location"> = window): void {
  const { pathname, search } = win.location;
  win.history.replaceState(null, "", `${pathname}${search}`);
}
