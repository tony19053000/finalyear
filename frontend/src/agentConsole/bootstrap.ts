/**
 * The launcher opens `http://127.0.0.1:47130/#c=<one-time code>` (or, when the
 * owner read the token file by hand, `#t=<token>`). The page trades it for the
 * HttpOnly session cookie once and removes it from the address bar and the
 * history at once - before the request, so a failed exchange leaves nothing
 * behind either. The fragment never reaches a server log (browsers do not send
 * it), and nothing is kept in browser storage.
 */
export interface LaunchCredential {
  code?: string;
  token?: string;
}

const MAX_LENGTH = 256;
const SAFE = /^[A-Za-z0-9_-]+$/;

/** The credential in `hash` (`#c=...` / `#t=...`), or null. */
export function readCredential(hash: string): LaunchCredential | null {
  const text = hash.startsWith("#") ? hash.slice(1) : hash;
  if (text === "") return null;
  const params = new URLSearchParams(text);
  const code = params.get("c");
  const token = params.get("t");
  if (code !== null && code.length <= MAX_LENGTH && SAFE.test(code)) return { code };
  if (token !== null && token.length <= MAX_LENGTH && SAFE.test(token)) return { token };
  return null;
}

/** Drop the fragment from the address bar without a new history entry. */
export function clearFragment(win: Pick<Window, "history" | "location"> = window): void {
  const { pathname, search } = win.location;
  win.history.replaceState(null, "", `${pathname}${search}`);
}
