/** The owner-settings form's pure logic (components/PolicyForm.tsx; unit-tested). */
import type { PolicyValues, PolicyView } from "./api";

const GIB = 1024 ** 3;

/** What the wizard proposes (D392, Q7/Q26): nothing runs until the owner says so. */
export const SAFE_DEFAULTS = {
  accept_jobs: false,
  max_cpu_percent: 50,
  max_gpu_jobs: 0,
  accept_gpu_sessions: false,
} as const satisfies PolicyValues;

export interface FormState {
  acceptJobs: boolean;
  cpuPercent: string;
  ramGib: string;
  cpuJobs: string;
  gpuJobs: boolean;
  gpuSessions: boolean;
  vramGib: string;
  unsandboxed: boolean;
  network: boolean;
  pauseBenchmarks: boolean;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function gibText(bytes: number | null): string {
  if (bytes === null) return "";
  const gib = bytes / GIB;
  return Number.isInteger(gib) ? String(gib) : gib.toFixed(1);
}

/** The form's starting values: the agent's effective policy, or the wizard's defaults over it. */
export function initialState(view: PolicyView, wizard: boolean): FormState {
  const p = view.policy;
  const bool = (key: string) => p[key] === true;
  return {
    acceptJobs: wizard ? SAFE_DEFAULTS.accept_jobs : bool("accept_jobs"),
    cpuPercent: wizard ? String(SAFE_DEFAULTS.max_cpu_percent) : String(num(p.max_cpu_percent) ?? ""),
    ramGib: gibText(num(p.max_ram_bytes)),
    cpuJobs: String(view.max_cpu_jobs),
    gpuJobs: wizard ? false : view.max_gpu_jobs !== 0,
    gpuSessions: wizard ? SAFE_DEFAULTS.accept_gpu_sessions : bool("accept_gpu_sessions"),
    vramGib: gibText(num(p.max_vram_bytes)),
    unsandboxed: bool("allow_unsandboxed"),
    network: bool("allow_network"),
    pauseBenchmarks: bool("pause_benchmarks"),
  };
}

export type Errors = Partial<Record<"cpuPercent" | "ramGib" | "cpuJobs" | "vramGib", string>>;

function wholeIn(text: string, min: number, max: number): number | null | "bad" {
  if (text.trim() === "") return null;
  const n = Number(text);
  if (!Number.isInteger(n) || n < min || n > max) return "bad";
  return n;
}

function gibBytes(text: string): number | null | "bad" {
  if (text.trim() === "") return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n <= 0 || n > 1024 * 1024) return "bad";
  return Math.round(n * GIB);
}

/** Form state -> the PUT body, or the field errors. */
export function toValues(form: FormState): { values: PolicyValues } | { errors: Errors } {
  const errors: Errors = {};
  const cpu = wholeIn(form.cpuPercent, 1, 100);
  if (cpu === "bad") errors.cpuPercent = "A whole number from 1 to 100, or empty for no limit.";
  const jobs = wholeIn(form.cpuJobs, 0, 64);
  if (jobs === "bad" || jobs === null) errors.cpuJobs = "A whole number from 0 to 64.";
  const ram = gibBytes(form.ramGib);
  if (ram === "bad") errors.ramGib = "A positive number of GiB, or empty for no limit.";
  const vram = gibBytes(form.vramGib);
  if (vram === "bad") errors.vramGib = "A positive number of GiB, or empty for no limit.";
  if (Object.keys(errors).length > 0) return { errors };
  return {
    values: {
      accept_jobs: form.acceptJobs,
      max_cpu_percent: cpu as number | null,
      max_ram_bytes: ram as number | null,
      max_cpu_jobs: jobs as number,
      max_gpu_jobs: form.gpuJobs ? null : 0,
      accept_gpu_sessions: form.gpuSessions,
      max_vram_bytes: vram as number | null,
      allow_unsandboxed: form.unsandboxed,
      allow_network: form.network,
      pause_benchmarks: form.pauseBenchmarks,
    },
  };
}

