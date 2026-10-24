/** Pure helpers of the console pages (unit-tested in src/test/agentConsole*.test.ts). */
import type { StatusTone } from "../lib/status";
import type { StatusView } from "./api";

/** D392: welcome -> join -> what this laptop shares -> checks -> done. */
export type WizardStep = "welcome" | "join" | "policy" | "checks" | "done";

/** Where the wizard starts: a laptop that already joined (terminal join, or a closed browser) resumes at the policy step. */
export function firstStep(status: Pick<StatusView, "joined">): WizardStep {
  return status.joined ? "policy" : "welcome";
}

const JOIN_CODE = /mf1-[A-Za-z0-9-]+/;

/** What a scanned QR text holds: the `mf1-...` code (alone or inside a longer text). */
export function joinCodeIn(text: string): string | null {
  const match = JOIN_CODE.exec(text.trim());
  return match === null ? null : match[0];
}

/** One line for the owner: is this laptop in the fabric right now? */
export function connectionSummary(status: StatusView): { tone: StatusTone; label: string } {
  if (status.phase === "stopped") return { tone: "failed", label: "Stopped" };
  if (status.phase === "waiting_for_join") return { tone: "unknown", label: "Not in a fabric" };
  if (status.phase === "starting" || status.phase === "stopping") return { tone: "busy", label: status.phase === "starting" ? "Starting…" : "Stopping…" };
  if (status.connection === "ONLINE") return { tone: "ok", label: "Connected to the fabric" };
  if (status.connection === "BACKOFF" || status.connection === "DISCOVERING") return { tone: "degraded", label: "Looking for the controller…" };
  return { tone: "busy", label: "Connecting…" };
}

