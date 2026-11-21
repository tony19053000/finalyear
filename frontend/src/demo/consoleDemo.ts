/**
 * DEMO MODE for the agent's local console:
 * `http://localhost:5173/src/agentConsole/index.html?demo=1` (vite dev server +
 * localhost only; never in a production build).
 * Answers `/console/v1/*` from memory, so the join wizard, Home, Settings,
 * Logs and Leave are seen without an agent on this laptop. The wizard walks
 * for real: discover finds a controller, any code joins, finishing marks
 * setup complete; Leave goes back to the start. `?demo=1&joined=1` opens
 * already set up. A reload resets it.
 * To remove: delete this file and the demo lines in src/agentConsole/main.tsx.
 */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** `?demo=1` on a local host, under the vite dev server only (never in a production build). */
export function consoleDemoRequested(): boolean {
  if (!import.meta.env.DEV) return false;
  if (!LOCAL_HOSTS.has(window.location.hostname)) return false;
  return new URLSearchParams(window.location.search).get("demo") === "1";
}

const GiB = 1024 ** 3;

export function installConsoleDemo(): void {
  const joinedAtStart = new URLSearchParams(window.location.search).get("joined") === "1";
  const st = {
    joined: joinedAtStart,
    setup: joinedAtStart,
    name: "asha-laptop",
    since: Date.now() / 1000 - 3 * 3600,
    policy: {
      accept_jobs: true,
      max_cpu_percent: 75,
      max_cpu_jobs: 2,
      max_ram_bytes: 8 * GiB,
      accept_gpu_sessions: true,
      max_gpu_jobs: 1,
      max_vram_bytes: 4 * GiB,
      allow_unsandboxed: false,
      allow_network: false,
      pause_benchmarks: false,
      allowed_users: [] as string[],
    } as Record<string, unknown>,
  };

  const status = () => ({
    hostname: "ASHA-LAPTOP",
    node_name: st.name,
    install_mode: "user",
    version: "0.17.4",
    joined: st.joined,
    setup_complete: st.setup,
    phase: st.joined ? "running" : "waiting_for_join",
    phase_since: st.since,
    node_id: st.joined ? "node_01JDEMO00000000000000ASHA" : null,
    fabric_id: st.joined ? "fab_01JDEMO0000000000000LAB" : null,
    controller: st.joined ? "lab-pc-07.local:8443" : null,
    connection: st.joined ? "ONLINE" : null,
    cert_not_after: st.joined ? new Date(Date.now() + 89 * 864e5).toISOString() : null,
    dashboard_url: st.joined ? "https://lab-pc-07.local:8443/" : null,
    session_id: st.joined ? "ses_demo" : null,
    identity_problem: null,
    stop_reason: null,
    upgrade: null,
  });

  const policyView = () => ({
    policy: st.policy,
    live_keys: Object.keys(st.policy),
    deprecated_keys: [],
    invalid: null,
    restart_needed: [],
    cpu_threads: 8,
    max_cpu_jobs: 4,
    max_gpu_jobs: 1,
    owner_activity_guard: true,
  });

  const checks = {
    checks: [
      { id: "python", title: "Python runtime", status: "ok", why: "Runs the work the hive sends.", fix: "", action: null },
      { id: "gpu", title: "NVIDIA GPU driver", status: "ok", why: "Lets this laptop lend its GPU.", fix: "", action: null },
      {
        id: "docker",
        title: "Docker access",
        status: "warn",
        why: "Sandboxed jobs run in containers.",
        fix: "Add your account to the docker group, then sign out and in.",
        action: "docker",
      },
      { id: "firewall", title: "Firewall", status: "ok", why: "The controller can reach this laptop.", fix: "", action: null },
    ],
  };

  const t0 = Date.now() - 600_000;
  const events: [string, string, Record<string, unknown>][] = [
    ["info", "agent.start", { version: "0.17.4" }],
    ["info", "controller.connected", { controller: "lab-pc-07.local:8443" }],
    ["info", "heartbeat.sent", { cpu: 0.21 }],
    ["info", "job.accepted", { job: "job_demo_split", chunk: 3 }],
    ["info", "job.chunk_done", { job: "job_demo_split", chunk: 3, s: 41.2 }],
    ["warning", "docker.unavailable", { fix: "add user to docker group" }],
    ["info", "job.accepted", { job: "job_demo_split", chunk: 7 }],
    ["info", "heartbeat.sent", { cpu: 0.64 }],
  ];
  const lines = events.map(([level, event, fields], i) => ({
    seq: i + 1,
    timestamp: new Date(t0 + i * 70_000).toISOString(),
    level,
    logger: "meshforge.agent",
    event,
    fields,
  }));

  const json = (body: unknown, code = 200) =>
    new Response(JSON.stringify(body), { status: code, headers: { "Content-Type": "application/json" } });
  const ok = (message = "") => json({ ok: true, message });
  const fail = (code: number, id: string, message: string, fix: string) => json({ error: { code: id, message, fix } }, code);

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(href, window.location.href);
    if (!url.pathname.startsWith("/console/v1")) return realFetch(input, init);
    const path = url.pathname.slice("/console/v1".length);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, unknown>) : {};
    await new Promise((r) => setTimeout(r, 250));
    switch (`${method} ${path}`) {
      case "POST /session":
      case "DELETE /session":
        return ok();
      case "GET /status":
        return json(status());
      case "GET /discover":
        return json({ controllers: [{ name: "lab-pc-07", host: "192.168.1.20", port: 8443 }], problem: null });
      case "POST /join": {
        const code = typeof body.code === "string" ? body.code.trim() : "";
        if (code === "") return fail(400, "bad_code", "That join code is empty.", "Copy the code from the dashboard's Invites page.");
        st.joined = true;
        st.since = Date.now() / 1000;
        return json({
          node_id: "node_01JDEMO00000000000000ASHA",
          fabric_id: "fab_01JDEMO0000000000000LAB",
          controller: "lab-pc-07.local:8443",
          cert_not_after: new Date(Date.now() + 90 * 864e5).toISOString(),
        });
      }
      case "POST /setup-complete":
        st.setup = true;
        return ok();
      case "GET /policy":
        return json(policyView());
      case "PUT /policy":
        Object.assign(st.policy, (body.values ?? {}) as Record<string, unknown>);
        return json(policyView());
      case "GET /node-name":
        return json({ name: st.name, hostname: "ASHA-LAPTOP", source: "console", applies_after_restart: false });
      case "PUT /node-name":
        if (typeof body.name === "string") st.name = body.name;
        return json({ name: st.name, hostname: "ASHA-LAPTOP", source: "console", applies_after_restart: true });
      case "GET /checks":
        return json(checks);
      case "GET /docker-access":
        return json({
          supported: true,
          command: "sudo usermod -aG docker $USER",
          explanation: "The MeshForge service cannot add itself to the docker group.",
          menu_action: "Open a terminal and run the command, then sign out and back in.",
        });
      case "GET /logs":
        return json({ capacity: 2000, lines });
      case "POST /restart":
        st.since = Date.now() / 1000;
        return ok("Restarting.");
      case "POST /leave":
        st.joined = false;
        st.setup = false;
        return ok("Left the hive.");
      default:
        return fail(404, "demo_not_found", `Demo has no ${method} ${path}.`, "");
    }
  };
}
