/**
 * Dark (default) / light theme (D175, Q5). The choice is the ONLY thing the
 * dashboard keeps in localStorage (checklist 25: no token, no data) - and
 * storage may be unavailable (privacy mode, blocked): every access is guarded.
 */
export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "mf-theme";

export function storedTheme(storage: Storage | null = safeLocalStorage()): Theme {
  try {
    return storage?.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function saveTheme(theme: Theme, storage: Storage | null = safeLocalStorage()): void {
  try {
    storage?.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Not persisted; the theme still applies for this page.
  }
}

export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  root.dataset.theme = theme;
}

function safeLocalStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
