import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Suspense, lazy, useCallback, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { consoleApi, consoleKeys, explain, type PolicyValues, type StatusView } from "../api";
import { ChecksList } from "../components/ChecksList";
import { INPUT, Problem } from "../components/fields";
import { PolicyForm } from "../components/PolicyForm";
import { firstStep, type WizardStep } from "../logic";

// jsQR is ~130 kB: loaded only when the owner opts in to scanning (Q6).
const QrScanner = lazy(() => import("../components/QrScanner").then((m) => ({ default: m.QrScanner })));

const ORDER: WizardStep[] = ["welcome", "join", "policy", "checks", "done"];
const TITLES: Record<WizardStep, string> = {
  welcome: "Welcome",
  join: "Join a fabric",
  policy: "What this laptop shares",
  checks: "Checks",
  done: "Done",
};

function Steps({ current }: { current: WizardStep }) {
  const at = ORDER.indexOf(current);
  return (
    <ol className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="Setup steps">
      {ORDER.map((step, i) => (
        <li key={step} aria-current={step === current ? "step" : undefined} className={i === at ? "font-semibold text-fg" : undefined}>
          {i + 1}. {TITLES[step]}
        </li>
      ))}
    </ol>
  );
}

function Welcome({ status, next }: { status: StatusView; next: () => void }) {
  return (
    <div className="flex flex-col gap-3 text-sm text-fg">
      <p>
        MeshForge lets the laptops of your group share their processors and graphics cards. This laptop (
        <span className="font-medium">{status.hostname}</span>) can join a fabric so others can send it work - only
        when you allow it, and only within limits you set.
      </p>
      <ul className="list-disc pl-5 text-fg-muted">
        <li>Nothing runs here until you turn on &quot;Run jobs&quot; in the next steps.</li>
        <li>You can change every setting later, or leave the fabric at any time.</li>
        <li>This page is only reachable from this laptop.</li>
      </ul>
      <div>
        <Button variant="primary" onClick={next}>
          Start
        </Button>
      </div>
    </div>
  );
}

function Join({ onJoined }: { onJoined: () => void }) {
  const [code, setCode] = useState("");
  const [host, setHost] = useState("");
  const [scanning, setScanning] = useState(false);
  const discover = useQuery({ queryKey: consoleKeys.discover, queryFn: consoleApi.discover, staleTime: 15_000 });
  const join = useMutation({
    mutationFn: () => consoleApi.join(code.trim(), host === "" ? null : host),
    onSuccess: onJoined,
  });
  const onScanned = useCallback((scanned: string) => {
    setScanning(false);
    setCode(scanned);
  }, []);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (code.trim() !== "") join.mutate();
  };
  const controllers = discover.data?.controllers ?? [];
  return (
    <form onSubmit={submit} className="flex flex-col gap-4" aria-label="Join a fabric">
      <p className="text-sm text-fg">
        Ask the fabric admin for a join code: on the controller&apos;s dashboard, <span className="font-medium">Settings &gt; Invites &gt; New invite</span>. Paste it here, or scan its QR code with this laptop&apos;s camera.
      </p>
      <div>
        <label htmlFor="join-code" className="text-sm font-medium text-fg">
          Join code
        </label>
        <input
          id="join-code"
          name="code"
          className={`${INPUT} mt-1 font-mono`}
          autoComplete="off"
          spellCheck={false}
          placeholder="mf1-…"
          value={code}
          onChange={(e) => {
            setCode(e.target.value);
          }}
        />
      </div>
      {scanning ? (
        <Suspense fallback={<p role="status" className="text-sm text-fg-muted">Loading the scanner…</p>}>
          <QrScanner
            onCode={onScanned}
            onCancel={() => {
              setScanning(false);
            }}
          />
        </Suspense>
      ) : (
        <div>
          <Button
            size="sm"
            onClick={() => {
              setScanning(true);
            }}
          >
            Scan QR code with the camera
          </Button>
        </div>
      )}
      <div>
        <label htmlFor="join-controller" className="text-sm font-medium text-fg">
          Controller
        </label>
        <p className="text-sm text-fg-muted">
          The code already says where the controller is. Pick one found on this network only if its address changed.
        </p>
        <select
          id="join-controller"
          className={`${INPUT} mt-1 max-w-md`}
          value={host}
          onChange={(e) => {
            setHost(e.target.value);
          }}
        >
          <option value="">The address in the code</option>
          {controllers.map((c) => (
            <option key={`${c.host}:${String(c.port)}`} value={c.host}>
              {c.name} ({c.host})
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-fg-subtle" role="status">
          {discover.isPending
            ? "Looking for controllers on this network…"
            : discover.isError
              ? "Could not look for controllers on this network."
              : controllers.length === 0
                ? discover.data.problem ?? "No controller answered on this network."
                : `${String(controllers.length)} found on this network.`}
        </p>
      </div>
      {join.isError && <Problem title="This laptop did not join" {...explain(join.error)} />}
      <div>
        <Button type="submit" variant="primary" loading={join.isPending} disabled={code.trim() === ""}>
          Join
        </Button>
      </div>
    </form>
  );
}

function PolicyStep({ status, next }: { status: StatusView; next: () => void }) {
  const client = useQueryClient();
  const policy = useQuery({ queryKey: consoleKeys.policy, queryFn: consoleApi.policy });
  const [name, setName] = useState(status.node_name);
  const save = useMutation({
    mutationFn: async (values: PolicyValues) => {
      if (name.trim() !== status.node_name) await consoleApi.saveNodeName(name);
      return consoleApi.savePolicy(values);
    },
    onSuccess: (view) => {
      client.setQueryData(consoleKeys.policy, view);
      void client.invalidateQueries({ queryKey: consoleKeys.status });
      next();
    },
  });
  if (policy.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading…
      </p>
    );
  }
  if (policy.isError) return <Problem title="Could not load the settings" {...explain(policy.error)} />;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-fg">
        Choose what this laptop shares. The safe choices are already set: <span className="font-medium">no jobs</span>,{" "}
        <span className="font-medium">GPU not shared</span>, <span className="font-medium">at most half the CPU</span> per job.
      </p>
      <div>
        <label htmlFor="node-name" className="text-sm font-medium text-fg">
          Name of this laptop
        </label>
        <p className="text-sm text-fg-muted">Shown in the fabric&apos;s dashboard next to its hostname ({status.hostname}). Optional.</p>
        <input
          id="node-name"
          className={`${INPUT} mt-1 max-w-md`}
          maxLength={64}
          value={name}
          onChange={(e) => {
            setName(e.target.value);
          }}
        />
      </div>
      <PolicyForm view={policy.data} wizard saving={save.isPending} submitLabel="Save and continue" onSubmit={save.mutate} />
      {save.isError && <Problem title="The settings were not saved" {...explain(save.error)} />}
    </div>
  );
}

function Done({ status, finish, finishing, error }: { status: StatusView; finish: () => void; finishing: boolean; error: unknown }) {
  return (
    <div className="flex flex-col gap-3 text-sm text-fg">
      <p>
        This laptop is in the fabric{status.fabric_id !== null && status.fabric_id !== undefined ? ` ${status.fabric_id}` : ""}. It
        appears in the fabric&apos;s dashboard within a few seconds.
      </p>
      {status.dashboard_url !== null && status.dashboard_url !== undefined && (
        <p>
          Dashboard:{" "}
          <a className="text-accent underline" href={status.dashboard_url} target="_blank" rel="noreferrer">
            {status.dashboard_url}
          </a>
        </p>
      )}
      <p className="text-fg-muted">You can open this page again from the MeshForge entry in the app menu.</p>
      {error !== null && error !== undefined && <Problem title="Could not finish" {...explain(error)} />}
      <div>
        <Button variant="primary" loading={finishing} onClick={finish}>
          Finish
        </Button>
      </div>
    </div>
  );
}

export function Wizard({ status }: { status: StatusView }) {
  const client = useQueryClient();
  const [step, setStep] = useState<WizardStep>(() => firstStep(status));
  const finish = useMutation({
    mutationFn: consoleApi.setupComplete,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: consoleKeys.status });
    },
  });
  const restart = useMutation({ mutationFn: consoleApi.restart });
  // The policy step cannot be skipped (Q26), and joining cannot be undone from here.
  const back = step === "join" ? "welcome" : step === "done" ? "checks" : null;
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-4 p-4">
      <Steps current={step} />
      <Card title={TITLES[step]}>
        {step === "welcome" && (
          <Welcome
            status={status}
            next={() => {
              setStep("join");
            }}
          />
        )}
        {step === "join" && (
          <Join
            onJoined={() => {
              void client.invalidateQueries({ queryKey: consoleKeys.status });
              setStep("policy");
            }}
          />
        )}
        {step === "policy" && (
          <PolicyStep
            status={status}
            next={() => {
              setStep("checks");
            }}
          />
        )}
        {step === "checks" && (
          <div className="flex flex-col gap-4">
            <p className="text-sm text-fg">
              What this laptop can offer right now. Nothing here stops it from joining: a missing item only means fewer
              kinds of jobs run here.
            </p>
            <ChecksList
              onRestart={() => {
                restart.mutate();
              }}
            />
            <div>
              <Button
                variant="primary"
                onClick={() => {
                  setStep("done");
                }}
              >
                Continue
              </Button>
            </div>
          </div>
        )}
        {step === "done" && (
          <Done
            status={status}
            finishing={finish.isPending}
            error={finish.error}
            finish={() => {
              finish.mutate();
            }}
          />
        )}
      </Card>
      {back !== null && (
        <div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setStep(back);
            }}
          >
            Back
          </Button>
        </div>
      )}
    </div>
  );
}
