import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import type { PolicyValues, PolicyView } from "../api";
import { initialState, toValues, type Errors, type FormState } from "../policyLogic";
import { NumberField, Toggle } from "./fields";

export interface PolicyFormProps {
  view: PolicyView;
  /** Start from the wizard's safe defaults instead of the current policy. */
  wizard?: boolean;
  saving: boolean;
  submitLabel: string;
  onSubmit: (values: PolicyValues) => void;
}

/** The owner's settings (0.17.5), each explained in one plain sentence. */
export function PolicyForm({ view, wizard = false, saving, submitLabel, onSubmit }: PolicyFormProps) {
  const [form, setForm] = useState<FormState>(() => initialState(view, wizard));
  const [errors, setErrors] = useState<Errors>({});
  const set = <K extends keyof FormState>(key: K) => (value: FormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  };
  const threads = view.cpu_threads;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const result = toValues(form);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    onSubmit(result.values);
  };
  return (
    <form onSubmit={submit} noValidate className="flex flex-col divide-y divide-border" aria-label="Owner settings">
      <fieldset className="pb-3">
        <legend className="mb-1 text-sm font-semibold text-fg">Jobs</legend>
        <Toggle
          name="accept_jobs"
          label="Run jobs from the fabric on this laptop"
          description="When on, programs that people in your fabric send may run here, within the limits below. Off: this laptop only joins, nothing runs."
          checked={form.acceptJobs}
          onChange={set("acceptJobs")}
        />
        <NumberField
          name="max_cpu_percent"
          label="Most CPU one job may use"
          description={`Share of this laptop's processor${threads !== null ? ` (${String(threads)} threads)` : ""} a single job can take. Empty = no limit.`}
          value={form.cpuPercent}
          onChange={set("cpuPercent")}
          suffix="%"
          min={1}
          max={100}
          error={errors.cpuPercent}
        />
        <NumberField
          name="max_ram_bytes"
          label="Most memory one job may use"
          description="A job that needs more is not sent here. Empty = no limit."
          value={form.ramGib}
          onChange={set("ramGib")}
          suffix="GiB"
          placeholder="no limit"
          step={0.5}
          error={errors.ramGib}
        />
        <NumberField
          name="max_cpu_jobs"
          label="Jobs at the same time"
          description="How many jobs without a GPU may run here at once."
          value={form.cpuJobs}
          onChange={set("cpuJobs")}
          min={0}
          max={64}
          error={errors.cpuJobs}
        />
      </fieldset>
      <fieldset className="py-3">
        <legend className="mb-1 text-sm font-semibold text-fg">GPU</legend>
        <Toggle
          name="gpu_jobs"
          label="Let jobs use this laptop's GPU"
          description="When on, jobs that need a graphics card may run here (one per GPU). Off: the GPU stays yours."
          checked={form.gpuJobs}
          onChange={set("gpuJobs")}
        />
        <Toggle
          name="accept_gpu_sessions"
          label="Share the GPU with programs on other laptops"
          description="Remote GPU and Combined GPU: a program on another laptop does its GPU work on this one while it runs."
          checked={form.gpuSessions}
          onChange={set("gpuSessions")}
        />
        <NumberField
          name="max_vram_bytes"
          label="Most GPU memory one job may ask for"
          description="Jobs asking for more are not sent here (the GPU itself cannot enforce it). Empty = no limit."
          value={form.vramGib}
          onChange={set("vramGib")}
          suffix="GiB"
          placeholder="no limit"
          step={0.5}
          error={errors.vramGib}
        />
      </fieldset>
      <fieldset className="py-3">
        <legend className="mb-1 text-sm font-semibold text-fg">Safety</legend>
        <Toggle
          name="allow_unsandboxed"
          label="Allow jobs without a container"
          description="Without Docker, jobs run as the MeshForge user directly on this laptop. Only turn this on if you trust everyone in the fabric."
          checked={form.unsandboxed}
          onChange={set("unsandboxed")}
        />
        <Toggle
          name="allow_network"
          label="Let jobs use the network"
          description="A job that asks for internet access may get it. Off: jobs run with no network."
          checked={form.network}
          onChange={set("network")}
        />
        <Toggle
          name="pause_benchmarks"
          label="Pause benchmark jobs"
          description={
            view.owner_activity_guard
              ? "Benchmark jobs already pause by themselves while you use this laptop; this pauses them all the time."
              : "No benchmark job starts here while this is on, and a running one stops."
          }
          checked={form.pauseBenchmarks}
          onChange={set("pauseBenchmarks")}
        />
      </fieldset>
      <div className="pt-3">
        <Button type="submit" variant="primary" loading={saving}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
