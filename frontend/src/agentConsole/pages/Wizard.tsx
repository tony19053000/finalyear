import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";

import { Button } from "../../components/Button";
import { consoleApi, consoleKeys, explain, type PolicyValues, type StatusView } from "../api";
import { ChecksList } from "../components/ChecksList";
import { INPUT, Panel, Problem } from "../components/fields";
import { PolicyForm } from "../components/PolicyForm";
import { firstStep, nodeNameProblem, type WizardStep } from "../logic";


const ORDER: WizardStep[] = ["welcome", "join", "policy", "checks", "done"];
const TITLES: Record<WizardStep, string> = {
  welcome: "Welcome",
  join: "Join a fabric",
  policy: "What this laptop shares",
  checks: "Checks",
  done: "Done",
};

const HINTS: Record<WizardStep, string> = {
  welcome: "What MeshForge does here",
  join: "Paste the join code",
  policy: "Limits you choose",
  checks: "What this laptop offers",
  done: "Ready",
};

/** The steps as a rail: done ones ticked, the current one in honey. */
function Steps({ current }: { current: WizardStep }) {
  const at = ORDER.indexOf(current);
  return (
    <ol className="ac-steps flex flex-col" aria-label="Setup steps">
      {ORDER.map((step, i) => (
        <li
          key={step}
          aria-current={step === current ? "step" : undefined}
          className="ac-step"
          data-state={i < at ? "done" : i === at ? "now" : "next"}
        >
          <span aria-hidden="true" className="ac-step-mark">
            {i < at ? "✓" : String(i + 1)}
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="ac-step-title">{TITLES[step]}</span>
            <span className="ac-step-hint">{HINTS[step]}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

/** One promise of the welcome step: a honey hex and a plain sentence. */
function Pledge({ children }: { children: ReactNode }) {
  return (
    <li className="flex items-start gap-4">
      <span aria-hidden="true" className="ac-hex mt-1.5" />
      <span>{children}</span>
    </li>
  );
}

function Welcome({ status, next }: { status: StatusView; next: () => void }) {
  return (
    <div className="flex flex-col gap-8">
      <p className="ac-lead">
        MeshForge lets the laptops of your group share their processors and graphics cards. This laptop,{" "}
        <span className="font-mono text-[0.9375rem] text-fg">{status.hostname}</span>, can join a fabric so others can send it work:
        only when you allow it, and only within the limits you set.
      </p>
      <ul className="flex flex-col gap-4 text-[0.9375rem] text-fg/85">
        <Pledge>Nothing runs here until you turn on &quot;Run jobs&quot; in the next steps.</Pledge>
        <Pledge>You can change every setting later, or leave the fabric at any time.</Pledge>
        <Pledge>This page is only reachable from this laptop.</Pledge>
      </ul>
      <div>
        <Button variant="primary" className="ac-go" onClick={next}>
          Start
        </Button>
      </div>
    </div>
  );
}

function Join({ onJoined }: { onJoined: () => void }) {
  const [code, setCode] = useState("");
  const [host, setHost] = useState("");
  const discover = useQuery({ queryKey: consoleKeys.discover, queryFn: consoleApi.discover, staleTime: 15_000 });
  const join = useMutation({
    mutationFn: () => consoleApi.join(code.trim(), host === "" ? null : host),
    onSuccess: onJoined,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (code.trim() !== "") join.mutate();
  };
  const controllers = discover.data?.controllers ?? [];
  return (
    <form onSubmit={submit} className="flex flex-col gap-8" aria-label="Join a fabric">
      <p className="ac-lead">
        Ask the fabric admin for a join code. On the controller&apos;s dashboard it is under{" "}
        <span className="text-fg">Settings → Invites → New invite</span>. Paste it here.
      </p>
      <div className="ac-field">
        <label htmlFor="join-code" className="nd-label">
          Join code
        </label>
        <input
          id="join-code"
          name="code"
          className={`${INPUT} ac-input-code`}
          autoComplete="off"
          spellCheck={false}
          placeholder="mf1-…"
          value={code}
          onChange={(e) => {
            setCode(e.target.value);
          }}
        />
      </div>
      <div className="ac-field">
        <label htmlFor="join-controller" className="nd-label">
          Controller
        </label>
        <p className="ac-row-help">The code already says where the controller is. Pick one found on this network only if its address changed.</p>
        <select
          id="join-controller"
          className={`${INPUT} ac-select max-w-md`}
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
        <p className="ac-note" role="status">
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
        <Button type="submit" variant="primary" className="ac-go" loading={join.isPending} disabled={code.trim() === ""}>
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
  const nameProblem = nodeNameProblem(name);
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
    <div className="flex flex-col gap-8">
      <p className="ac-lead">
        Choose what this laptop shares. The safe choices are already set: <span className="font-medium">no jobs</span>,{" "}
        <span className="font-medium">GPU not shared</span>, <span className="font-medium">at most half the CPU</span> per job.
      </p>
      <div className="ac-field">
        <label htmlFor="node-name" className="nd-label">
          Name of this laptop
        </label>
        <p className="ac-row-help">Shown in the fabric&apos;s dashboard next to its hostname ({status.hostname}). Optional.</p>
        <input
          id="node-name"
          className={`${INPUT} max-w-md`}
          value={name}
          aria-invalid={nameProblem !== null || undefined}
          aria-describedby={nameProblem !== null ? "node-name-error" : undefined}
          onChange={(e) => {
            setName(e.target.value);
          }}
        />
        {nameProblem !== null && (
          <p id="node-name-error" role="alert" className="mt-2 text-sm text-status-failed">
            {nameProblem}
          </p>
        )}
      </div>
      <PolicyForm
        view={policy.data}
        wizard
        saving={save.isPending}
        submitLabel="Save and continue"
        onSubmit={(values) => {
          if (nameProblem === null) save.mutate(values);
        }}
      />
      {save.isError && <Problem title="The settings were not saved" {...explain(save.error)} />}
    </div>
  );
}

function Done({ status, finish, finishing, error }: { status: StatusView; finish: () => void; finishing: boolean; error: unknown }) {
  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center gap-5">
        <span aria-hidden="true" className="ac-done-hex">
          {"✓"}
        </span>
        <p className="ac-lead">
          This laptop is in the fabric
          {status.fabric_id !== null && status.fabric_id !== undefined ? (
            <>
              {" "}
              <span className="font-mono text-[0.9375rem] text-fg">{status.fabric_id}</span>
            </>
          ) : null}
          . It appears in the fabric&apos;s dashboard within a few seconds.
        </p>
      </div>
      {status.dashboard_url !== null && status.dashboard_url !== undefined && (
        <div className="ac-field">
          <span className="nd-label">Dashboard</span>
          <a className="ac-link" href={status.dashboard_url} target="_blank" rel="noreferrer">
            {status.dashboard_url}
          </a>
        </div>
      )}
      <p className="ac-note">You can open this page again from the MeshForge entry in the app menu.</p>
      {error !== null && error !== undefined && <Problem title="Could not finish" {...explain(error)} />}
      <div>
        <Button variant="primary" className="ac-go" loading={finishing} onClick={finish}>
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
    <div className="ac-wizard grid grid-cols-1 gap-10 lg:grid-cols-[16rem_minmax(0,1fr)] lg:gap-14">
      <aside className="flex flex-col gap-8">
        <div>
          <p className="nd-label">Set up</p>
          <p className="mt-3 font-display text-[2.25rem] leading-none font-bold tracking-[-0.035em] text-fg">
            Join the <span className="font-serif font-normal italic text-[#ffc93c]">hive.</span>
          </p>
        </div>
        <Steps current={step} />
      </aside>
      <div className="flex min-w-0 flex-col gap-5">
      <Panel title={TITLES[step]} actions={<span className="ac-note">{`Step ${String(ORDER.indexOf(step) + 1)} of ${String(ORDER.length)}`}</span>}>
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
          <div className="flex flex-col gap-8">
            <p className="ac-lead">
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
                className="ac-go"
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
      </Panel>
      {back !== null && (
        <div>
          <Button
            variant="ghost"
            size="sm"
            className="ac-quiet"
            onClick={() => {
              setStep(back);
            }}
          >
            Back
          </Button>
        </div>
      )}
      </div>
    </div>
  );
}
