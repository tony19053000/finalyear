/** Join class names, skipping falsy parts (a tiny `clsx`; no dependency). */
export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
