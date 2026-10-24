import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { useToast } from "../../components/toastContext";
import { consoleApi, consoleKeys, explain, type PolicyValues } from "../api";
import { INPUT, Problem } from "../components/fields";
import { PolicyForm } from "../components/PolicyForm";

const RESTART_WHY: Record<string, string> = {
  accept_jobs: "MeshForge was started with jobs off, so it has not set up a way to run them yet.",
  allow_unsandboxed: "MeshForge was started without container-free jobs, so it has not set up that runtime yet.",
};

function NodeName() {
  const client = useQueryClient();
  const toast = useToast();
  const current = useQuery({ queryKey: consoleKeys.nodeName, queryFn: consoleApi.nodeName });
  const [draft, setDraft] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (name: string) => consoleApi.saveNodeName(name),
    onSuccess: (view) => {
      client.setQueryData(consoleKeys.nodeName, view);
      void client.invalidateQueries({ queryKey: consoleKeys.status });
      setDraft(null);
      toast({
        title: "Name saved",
        description: view.applies_after_restart ? "The dashboard shows it after MeshForge reconnects (Restart on the Home page)." : undefined,
        tone: "ok",
      });
    },
  });
  if (current.isPending) return <p role="status" className="text-sm text-fg-muted">Loading…</p>;
  if (current.isError) return <Problem title="Could not load the name" {...explain(current.error)} />;
  const value = draft ?? current.data.name;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(value);
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" aria-label="Name of this laptop">
      <label htmlFor="settings-node-name" className="text-sm font-medium text-fg">
        Name of this laptop
      </label>
      <p className="text-sm text-fg-muted">Shown in the fabric&apos;s dashboard next to its hostname ({current.data.hostname}). Empty = the hostname only.</p>
      <div className="flex max-w-md flex-wrap gap-2">
        <input
          id="settings-node-name"
          className={`${INPUT} min-w-0 flex-1`}
          maxLength={64}
          value={value}
          onChange={(e) => { setDraft(e.target.value); }}
        />
        <Button type="submit" loading={save.isPending} disabled={draft === null}>
          Save name
        </Button>
      </div>
      {save.isError && <Problem title="The name was not saved" {...explain(save.error)} />}
    </form>
  );
}

export function Settings() {
  const client = useQueryClient();
  const toast = useToast();
  const policy = useQuery({ queryKey: consoleKeys.policy, queryFn: consoleApi.policy });
  const [restartNeeded, setRestartNeeded] = useState<string[]>([]);
  const [formKey, setFormKey] = useState(0);
  const save = useMutation({
    mutationFn: (values: PolicyValues) => consoleApi.savePolicy(values),
    onSuccess: (view) => {
      client.setQueryData(consoleKeys.policy, view);
      setRestartNeeded(view.restart_needed);
      setFormKey((k) => k + 1);
      toast({ title: "Settings saved", description: "MeshForge applies them within a few seconds.", tone: "ok" });
    },
  });
  const restart = useMutation({
    mutationFn: consoleApi.restart,
    onSuccess: () => {
      setRestartNeeded([]);
      toast({ title: "Restarting MeshForge's connection", tone: "ok" });
    },
  });
  return (
    <div className="flex flex-col gap-4">
      <Card title="Name">
        <NodeName />
      </Card>
      <Card title="What this laptop shares">
        {policy.isPending ? (
          <p role="status" className="text-sm text-fg-muted">Loading…</p>
        ) : policy.isError ? (
          <Problem title="Could not load the settings" {...explain(policy.error)} />
        ) : (
          <div className="flex flex-col gap-3">
            {policy.data.invalid !== null && (
              <Problem
                title="The saved settings are broken, so this laptop accepts no jobs"
                message={policy.data.invalid}
                fix="Save the settings below to replace them."
              />
            )}
            {restartNeeded.length > 0 && (
              <div role="status" className="flex flex-col gap-2 rounded-lg border border-status-degraded bg-status-degraded-soft px-4 py-3">
                <p className="text-sm text-fg">One restart is needed for this change to take effect.</p>
                <ul className="list-disc pl-5 text-sm text-fg-muted">
                  {restartNeeded.map((key) => (
                    <li key={key}>{RESTART_WHY[key] ?? key}</li>
                  ))}
                </ul>
                <div>
                  <Button size="sm" variant="primary" loading={restart.isPending} onClick={() => { restart.mutate(); }}>
                    Restart now
                  </Button>
                </div>
              </div>
            )}
            <PolicyForm key={formKey} view={policy.data} saving={save.isPending} submitLabel="Save settings" onSubmit={save.mutate} />
            {save.isError && <Problem title="The settings were not saved" {...explain(save.error)} />}
          </div>
        )}
      </Card>
    </div>
  );
}
