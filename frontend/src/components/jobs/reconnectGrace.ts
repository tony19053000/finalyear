/** V1.0 G10 (D445): "waiting for node to reconnect (Ns)" while the attempt's
 * node lost its lease and the controller holds the attempt for it; "" otherwise. */
export function reconnectGraceText(
  until: string | null | undefined,
  now: number = Date.now(),
): string {
  if (until == null || until === "") return "";
  const end = Date.parse(until);
  if (Number.isNaN(end)) return "";
  const left = Math.max(0, Math.round((end - now) / 1000));
  return `waiting for node to reconnect (${String(left)}s)`;
}
