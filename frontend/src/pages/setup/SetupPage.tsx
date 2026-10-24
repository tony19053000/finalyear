/**
 * "Create your fabric" - the controller's one-time setup page (V0.17 G2, D392).
 *
 * Opened from the link `meshforge-controller setup url` prints (the package's
 * install output shows it): `http://127.0.0.1:47110/setup#token=...`. Steps:
 *
 * 1. the token in the URL fragment is exchanged for an HttpOnly session cookie
 *    and removed from the address bar at once;
 * 2. the controller's checks and the address laptops will join are shown;
 * 3. the first admin's name and password (the rule is shown and pre-checked;
 *    the controller decides);
 * 4. done: the first join code (text + QR, shown only now), the CA fingerprint,
 *    and the dashboard's address - the controller restarts into its normal mode.
 *
 * On a controller that is already set up every call is 410 and the page says
 * so with a link to sign in.
 */
import { useEffect, useId, useState, type FormEvent } from "react";

import { ApiError, NetworkError } from "../../api/errors";
import { LogoMark } from "../../app/icons";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { CopyField } from "../../components/CopyField";
import { QrCode } from "../../components/QrCode";
import { PRODUCT_NAME } from "../../config";
import {
  ADMIN_MIN_CHARS,
  completeSetup,
  getSetupInfo,
  getSetupState,
  isGone,
  openSession,
  passwordProblem,
  tokenFromHash,
  type DoctorCheck,
  type SetupInfo,
  type SetupResult,
} from "./setupApi";

type Phase =
  | { kind: "loading" }
  | { kind: "need-link"; message?: string }
  | { kind: "gone" }
  | { kind: "error"; message: string }
  | { kind: "form"; info: SetupInfo }
  | { kind: "done"; result: SetupResult };

function messageOf(error: unknown): string {
  if (error instanceof ApiError || error instanceof NetworkError) return error.message;
  return "Something went wrong. Reload the page and try again.";
}

const STATUS_LABEL: Record<DoctorCheck["status"], string> = { ok: "OK", warn: "Note", fail: "Problem" };
const STATUS_CLASS: Record<DoctorCheck["status"], string> = {
  ok: "text-status-ok",
  warn: "text-status-degraded",
  fail: "text-status-failed",
};

export function SetupPage() {
  useDocumentTitle("Create your fabric");
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const token = tokenFromHash(window.location.hash);
    if (token !== null) {
      // The token must not stay in the address bar or the history.
      window.history.replaceState(null, "", window.location.pathname);
    }
    const run = async () => {
      try {
        if (token !== null) {
          await openSession(token);
        } else {
          const state = await getSetupState();
          if (!state.session) {
            if (!cancelled) setPhase({ kind: "need-link" });
            return;
          }
        }
        const info = await getSetupInfo();
        if (!cancelled) setPhase({ kind: "form", info });
      } catch (error) {
        if (cancelled) return;
        if (isGone(error)) setPhase({ kind: "gone" });
        else if (error instanceof ApiError && error.status === 401) {
          setPhase({ kind: "need-link", message: error.message });
        } else setPhase({ kind: "error", message: messageOf(error) });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex min-h-screen justify-center bg-bg px-4 py-10">
      <main className="flex w-full max-w-2xl flex-col gap-5">
        <div className="flex items-center gap-2">
          <LogoMark />
          <span className="text-lg font-semibold text-fg">{PRODUCT_NAME}</span>
        </div>
        <h1 className="text-xl font-semibold text-fg">Create your fabric</h1>
        {phase.kind === "loading" && (
          <p role="status" className="text-sm text-fg-muted">
            Checking the setup link…
          </p>
        )}
        {phase.kind === "need-link" && <NeedLink message={phase.message} />}
        {phase.kind === "gone" && <AlreadySetUp />}
        {phase.kind === "error" && (
          <p role="alert" className="text-sm text-status-failed">
            {phase.message}
          </p>
        )}
        {phase.kind === "form" && (
          <SetupForm
            info={phase.info}
            onDone={(result) => {
              setPhase({ kind: "done", result });
            }}
            onGone={() => {
              setPhase({ kind: "gone" });
            }}
          />
        )}
        {phase.kind === "done" && <Done result={phase.result} />}
      </main>
    </div>
  );
}

function NeedLink({ message }: { message?: string | undefined }) {
  return (
    <Card title="Open your setup link">
      <div className="flex flex-col gap-3 text-sm text-fg">
        {message !== undefined && <p role="alert">{message}</p>}
        <p>
          This controller is not set up yet. Its setup link was printed when it was installed. To print it again, run this
          on the controller and open the link in a browser on that machine:
        </p>
        <CodeBlock label="print the setup link" code="sudo meshforge-controller setup url" />
        <p className="text-fg-muted">The link works once; it is never sent over the network.</p>
      </div>
    </Card>
  );
}

function AlreadySetUp() {
  return (
    <Card title="This controller is already set up">
      <div className="flex flex-col gap-3 text-sm text-fg">
        <p>The fabric exists, so this page is closed for good. Sign in on the dashboard.</p>
        <div>
          <a className="text-accent underline" href="/">
            Go to the dashboard
          </a>
        </div>
      </div>
    </Card>
  );
}

function Checks({ info }: { info: SetupInfo }) {
  const checks = info.checks?.checks ?? [];
  return (
    <Card title="This controller">
      <div className="flex flex-col gap-3 text-sm text-fg">
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
          <dt className="text-fg-muted">Computer</dt>
          <dd className="font-mono">{info.hostname}</dd>
          <dt className="text-fg-muted">Laptops join at</dt>
          <dd className="font-mono">{info.enroll_address ?? "not known yet"}</dd>
          <dt className="text-fg-muted">Dashboard</dt>
          <dd className="font-mono">{info.dashboard_url}</dd>
        </dl>
        {info.address_problem !== null && (
          <p role="alert" className="text-status-degraded">
            {info.address_problem}
          </p>
        )}
        {info.loopback_only && (
          <p role="alert" className="text-status-degraded">
            The controller listens on this computer only (127.0.0.1), so other laptops cannot reach it. Put this
            computer&apos;s network address in <span className="font-mono">{info.config_file}</span> first, restart the
            controller, and open the setup link again.
          </p>
        )}
        {checks.length > 0 && (
          <ul aria-label="checks" className="flex flex-col gap-1">
            {checks.map((check) => (
              <li key={check.name} className="flex gap-2">
                <span className={`w-16 shrink-0 font-medium ${STATUS_CLASS[check.status]}`}>
                  {STATUS_LABEL[check.status]}
                </span>
                <span>
                  {check.name}: {check.detail}
                  {check.status !== "ok" && check.hint !== "" && (
                    <span className="block text-fg-muted">{check.hint}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function SetupForm({
  info,
  onDone,
  onGone,
}: {
  info: SetupInfo;
  onDone: (result: SetupResult) => void;
  onGone: () => void;
}) {
  const [admin, setAdmin] = useState("admin");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorId = useId();
  const ruleId = useId();
  const local = password === "" ? null : passwordProblem(admin, password, repeat);

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || local !== null || password === "") return;
    setBusy(true);
    setError(null);
    completeSetup(admin.trim(), password)
      .then((result) => {
        setPassword("");
        setRepeat("");
        onDone(result);
      })
      .catch((err: unknown) => {
        setBusy(false);
        if (isGone(err)) onGone();
        else setError(messageOf(err));
      });
  };

  return (
    <>
      <Checks info={info} />
      <Card title="The first admin">
        <form onSubmit={onSubmit} aria-label="create the fabric" className="flex flex-col gap-4" noValidate>
          <p className="text-sm text-fg-muted">
            This account manages the fabric: it invites laptops and can run work on them. More users can be added later on
            the dashboard.
          </p>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="setup-admin" className="text-sm font-medium text-fg">
              Admin user name
            </label>
            <input
              id="setup-admin"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={64}
              value={admin}
              onChange={(e) => {
                setAdmin(e.target.value);
              }}
              className="h-9 rounded-md border border-border-strong bg-surface-sunken px-3 text-base text-fg"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="setup-password" className="text-sm font-medium text-fg">
              Password
            </label>
            <input
              id="setup-password"
              type="password"
              autoComplete="new-password"
              value={password}
              aria-describedby={ruleId}
              onChange={(e) => {
                setPassword(e.target.value);
              }}
              className="h-9 rounded-md border border-border-strong bg-surface-sunken px-3 text-base text-fg"
            />
            <p id={ruleId} className="text-xs text-fg-subtle">
              At least {ADMIN_MIN_CHARS} characters, not containing the user name. A few unrelated words make a good
              password.
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="setup-repeat" className="text-sm font-medium text-fg">
              Repeat the password
            </label>
            <input
              id="setup-repeat"
              type="password"
              autoComplete="new-password"
              value={repeat}
              onChange={(e) => {
                setRepeat(e.target.value);
              }}
              className="h-9 rounded-md border border-border-strong bg-surface-sunken px-3 text-base text-fg"
            />
          </div>
          {(local !== null || error !== null) && (
            <p id={errorId} role="alert" className="text-sm text-status-failed">
              {error ?? local}
            </p>
          )}
          <div>
            <Button
              type="submit"
              variant="primary"
              loading={busy}
              disabled={admin.trim() === "" || password === "" || local !== null}
            >
              Create the fabric
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}

function Done({ result }: { result: SetupResult }) {
  return (
    <>
      <Card title="Your fabric is ready">
        <div className="flex flex-col gap-3 text-sm text-fg">
          <p role="status">
            The fabric CA and the admin <span className="font-mono">{result.admin}</span> were created. The controller is
            restarting now with its secure (HTTPS) address; this page closes for good.
          </p>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
            <dt className="text-fg-muted">Fabric</dt>
            <dd className="font-mono">{result.fabric_id}</dd>
            <dt className="text-fg-muted">CA fingerprint</dt>
            <dd className="break-all font-mono">{result.join_fingerprint}</dd>
          </dl>
          <p>
            Sign in at{" "}
            <a className="font-mono text-accent underline" href={result.dashboard_url}>
              {result.dashboard_url}
            </a>
            . The browser warns about the certificate until this computer trusts the fabric CA (
            <span className="font-mono">meshforge-controller ca trust-helper</span> shows how); check that the fingerprint
            it shows matches the one above.
          </p>
        </div>
      </Card>
      <Card title="Add the first laptop">
        {result.invite !== null ? (
          <div className="flex flex-col gap-3 text-sm text-fg">
            <CopyField label="Join code - shown only now" value={result.invite.code} />
            <div className="flex justify-center">
              <QrCode text={result.invite.code} label="QR code of the join code" size={208} className="rounded-md" />
            </div>
            <p>
              On the new laptop, open the MeshForge app and paste (or scan) this code. It works once, until{" "}
              {result.invite.expires_at}. More codes: the dashboard&apos;s Invites page.
            </p>
          </div>
        ) : (
          <p role="alert" className="text-sm text-status-degraded">
            {result.invite_problem ?? "Make a join code on the dashboard's Invites page."}
          </p>
        )}
      </Card>
    </>
  );
}
