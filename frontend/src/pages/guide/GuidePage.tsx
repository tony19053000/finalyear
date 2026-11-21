/**
 * Product guide (`/guide`, opened by the top bar's bell): what hive is, the
 * ways a job can run, how a job flows, what each page shows and the words
 * the dashboard uses. A reading page, not a dashboard: one calm centred
 * column on the page background, hairline separators, mono captions
 * (`nd-label`), small line diagrams. Styles: the `.gd-*` block in
 * styles/index.css (no inline styles: CSP).
 *
 * The copy follows docs/PROJECT_SUMMARY.md and CLAUDE.md section 2: Local,
 * Mode 1 and Mode 3 are built; Mode 2 and Mode 4 are designed, not built
 * yet, and the page says so.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";

/* ------------------------------------------------------------ diagrams */

/** A laptop outline: screen at (x, y), 28 x 18, and its base line. */
function Laptop({ x, y, children }: { x: number; y: number; children?: ReactNode }) {
  return (
    <g>
      <rect className="gd-ln" x={x} y={y} width={28} height={18} rx={2} />
      <path className="gd-ln" d={`M${String(x - 4)} ${String(y + 21.5)}h36`} />
      {children}
    </g>
  );
}

/** The program that stays on your laptop: a small block inside the screen. */
function Program({ x, y }: { x: number; y: number }) {
  return <rect className="gd-blk" x={x + 8} y={y + 5} width={12} height={8} rx={1} />;
}

/** A GPU chip inside a laptop's screen. */
function Chip({ x, y }: { x: number; y: number }) {
  return (
    <g>
      <rect className="gd-ln" x={x + 9} y={y + 4} width={10} height={10} rx={1} />
      <path className="gd-ln" d={`M${String(x + 12)} ${String(y + 2)}v2m4-2v2M${String(x + 12)} ${String(y + 14)}v2m4-2v2`} />
    </g>
  );
}

/** The task travelling: a small honey square on a line. */
function Task({ cx, cy, size = 6 }: { cx: number; cy: number; size?: number }) {
  return <rect className="gd-mvf" x={cx - size / 2} y={cy - size / 2} width={size} height={size} rx={1} />;
}

const FIG_LABEL: Record<string, string> = {
  offload: "Diagram: your laptop sends the whole task to one other laptop; only the output comes back.",
  split: "Diagram: your laptop sends one part of the task to each of three laptops.",
  remote: "Diagram: the program stays on your laptop; its GPU calculations go to another laptop's GPU.",
  combined: "Diagram: the program stays on your laptop; the GPUs of three laptops work for it as one.",
};

/** Mode 1: one laptop, task sent away. */
function OffloadFigure() {
  return (
    <svg className="gd-fig" viewBox="0 0 200 64" role="img" aria-label={FIG_LABEL.offload}>
      <Laptop x={14} y={18} />
      <Laptop x={158} y={18} />
      <path className="gd-mv" d="M50 24H150" />
      <path className="gd-mv" d="M146 21l4 3-4 3" />
      <Task cx={100} cy={24} />
      <path className="gd-ln gd-dash" d="M150 34H50" />
      <path className="gd-ln" d="M54 31l-4 3 4 3" />
      <text className="gd-tx" x={28} y={56} textAnchor="middle">YOU</text>
      <text className="gd-tx" x={172} y={56} textAnchor="middle">B</text>
      <text className="gd-tx" x={100} y={46} textAnchor="middle">OUTPUT</text>
    </svg>
  );
}

/** Mode 3: many laptops, task split and sent away. */
function SplitFigure() {
  const ys = [2, 23, 44];
  return (
    <svg className="gd-fig" viewBox="0 0 200 68" role="img" aria-label={FIG_LABEL.split}>
      <Laptop x={14} y={23} />
      {ys.map((y) => (
        <g key={y}>
          <Laptop x={158} y={y} />
          <path className="gd-mv" d={`M50 32C100 32 104 ${String(y + 9)} 150 ${String(y + 9)}`} />
          {/* on the curve at t = 0.7 */}
          <Task cx={117.6} cy={6.912 + 0.784 * (y + 9)} size={5} />
        </g>
      ))}
      <text className="gd-tx" x={28} y={64} textAnchor="middle">YOU</text>
    </svg>
  );
}

/** Mode 2: one laptop, program stays; GPU calculations go to B's GPU. */
function RemoteGpuFigure() {
  return (
    <svg className="gd-fig" viewBox="0 0 200 64" role="img" aria-label={FIG_LABEL.remote}>
      <Laptop x={14} y={18}>
        <Program x={14} y={18} />
      </Laptop>
      <Laptop x={158} y={18}>
        <Chip x={158} y={18} />
      </Laptop>
      <path className="gd-mv gd-dash" d="M50 27H150" />
      <text className="gd-tx" x={28} y={56} textAnchor="middle">YOU</text>
      <text className="gd-tx" x={172} y={56} textAnchor="middle">B · GPU</text>
      <text className="gd-tx" x={100} y={46} textAnchor="middle">GPU CALLS</text>
    </svg>
  );
}

/** Mode 4: many laptops, program stays; their GPUs act as one. */
function CombinedGpuFigure() {
  const ys = [2, 23, 44];
  return (
    <svg className="gd-fig" viewBox="0 0 200 68" role="img" aria-label={FIG_LABEL.combined}>
      <Laptop x={14} y={23}>
        <Program x={14} y={23} />
      </Laptop>
      {ys.map((y) => (
        <g key={y}>
          <Laptop x={158} y={y}>
            <Chip x={158} y={y} />
          </Laptop>
          <path className="gd-mv gd-dash" d={`M50 32C100 32 104 ${String(y + 9)} 150 ${String(y + 9)}`} />
        </g>
      ))}
      <path className="gd-ln" d="M193 11h3v42h-3" />
      <text className="gd-tx" x={28} y={64} textAnchor="middle">YOU</text>
    </svg>
  );
}

/** Local: one laptop, the job stays on it. */
function LocalFigure() {
  return (
    <svg className="gd-fig-sm" viewBox="0 0 40 26" aria-hidden="true">
      <Laptop x={6} y={2}>
        <rect className="gd-mvf" x={17} y={8} width={6} height={6} rx={1} />
      </Laptop>
    </svg>
  );
}

/* ------------------------------------------------------------ content */

interface Way {
  mode: string;
  name: string;
  /** What the dashboard calls it (jobs list, job page). */
  shown?: string;
  /** The matrix position, for the one-column layout. */
  axis: string;
  built: boolean;
  figure: ReactNode;
  text: ReactNode;
}

/** Row-major: (one, sent away) (many, sent away) / (one, stays) (many, stays). */
const WAYS: readonly Way[] = [
  {
    mode: "Mode 1",
    name: "Job Offloading",
    shown: "Sent away",
    axis: "One laptop · task is sent away",
    built: true,
    figure: <OffloadFigure />,
    text: "The whole job is sent to one other laptop and runs there. Only the output comes back to you.",
  },
  {
    mode: "Mode 3",
    name: "Task Splitting",
    shown: "Split",
    axis: "Many laptops · task is sent away",
    built: true,
    figure: <SplitFigure />,
    text: (
      <>
        The job is cut into parts. Several laptops each run a part at the same time, then the results are combined. A
        slower laptop, or one with no GPU, gets a smaller share. Built on Mode 1.
      </>
    ),
  },
  {
    mode: "Mode 2",
    name: "Remote GPU",
    axis: "One laptop · program stays on yours",
    built: false,
    figure: <RemoteGpuFigure />,
    text: "Your program keeps running on your laptop. Only its GPU calculations run on another laptop's GPU. Designed, not built yet.",
  },
  {
    mode: "Mode 4",
    name: "Combined GPU",
    axis: "Many laptops · program stays on yours",
    built: false,
    figure: <CombinedGpuFigure />,
    text: (
      <>
        The GPUs of several laptops act as one larger GPU for a program on your laptop, for PyTorch AI work too heavy for
        one GPU. Data has to cross the network, so the gain is more than one laptop alone but less than the sum. Built on
        Mode 2. Designed, not built yet.
      </>
    ),
  },
];

const STEPS: readonly { title: string; text: ReactNode }[] = [
  {
    title: "You submit",
    text: (
      <>
        A project folder or .zip with <code className="gd-code">compute.yaml</code> or <code className="gd-code">main.py</code>,
        from Jobs or the <code className="gd-code">compute</code> command line.
      </>
    ),
  },
  {
    title: "The planner picks",
    text: "It estimates each way to run the job from the laptops' hardware, current load and network, corrected by past runs, and picks the best. The reason is kept with the job.",
  },
  {
    title: "It runs",
    text: "The agents run the work within each owner's policy. If a laptop drops out mid-job, its share moves to the others.",
  },
  {
    title: "Results come back",
    text: "Output and logs return to you. The measured times are recorded, and the planner learns from them for the next job.",
  },
];

interface PageEntry {
  to: string;
  name: string;
  adminOnly?: boolean;
  text: ReactNode;
  sub?: readonly { name: string; text: string }[];
}

const PAGES: readonly PageEntry[] = [
  {
    to: "/",
    name: "Overview",
    text: "The fabric at a glance: every laptop and its state, resource use, load over time, the newest job's progress and estimated speed-up, and alerts for admins.",
  },
  {
    to: "/nodes",
    name: "Nodes",
    text: "Each laptop: state, hardware, live CPU, RAM and GPU, network, caches and its owner policy.",
  },
  {
    to: "/jobs",
    name: "Jobs",
    text: "Every job with its state, how it runs and its progress. Open one for its plan, logs and results, or submit a new one.",
  },
  {
    to: "/benchmarks",
    name: "Benchmarks",
    adminOnly: true,
    text: "Admins only. Campaigns that run the same workloads in several ways, many times, and compare the measured medians. This is the data the planner learns from.",
  },
  {
    to: "/planner",
    name: "Planner",
    text: "How the planner corrects itself and how good its picks are.",
    sub: [
      { name: "Calibration", text: "the correction factors fitted from measured runs, and every version with why it was kept or rejected." },
      { name: "Evaluation", text: "reports that replay past runs under each planning policy and score the picks by regret." },
      { name: "Models", text: "the machine-learning models that predict run times, with predicted against actual." },
    ],
  },
  {
    to: "/settings",
    name: "Settings",
    text: "Your account, API tokens, job secrets and shared data inputs. Admins also manage invites and users.",
  },
];

const TERMS: readonly { term: ReactNode; text: ReactNode }[] = [
  {
    term: "Node",
    text: "A laptop in the fabric, running the hive agent. The dashboard says node; it means laptop.",
  },
  {
    term: "Job",
    text: "One piece of work you submit: a project the fabric runs, locally, sent away or split.",
  },
  {
    term: "Owner policy",
    text: "Limits the owner sets on the laptop itself: whether it accepts jobs, CPU, RAM and VRAM limits, only when plugged in, quiet hours. The Nodes page shows them.",
  },
  {
    term: "Estimate vs measured",
    text: "An estimate is the planner's prediction before a run. Measured is what the run really took. The dashboard keeps them apart.",
  },
  {
    term: <span className="font-mono">est.</span>,
    text: "Marks a figure that is an estimate, not a measurement: a speed-up from the plan, or progress from elapsed time against the estimated time.",
  },
  {
    term: "Speed-up",
    text: "The local estimate divided by the chosen way's estimate. For a job that stays local: the time saved against the best other way.",
  },
  {
    term: "Calibration",
    text: "Correction factors, fitted from measured runs, that bring the planner's estimates closer to reality. A new fit is used only if it does better on runs it was not fitted on.",
  },
  {
    term: "Regret",
    text: "How much slower the planner's pick was than the fastest option measured for the same work, in percent. 0 % means it picked the best.",
  },
];

/* ------------------------------------------------------------ page */

function Section({ id, label, title, children }: { id: string; label: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="gd-sec nd-hair-t">
      <div className="gd-sec-head">
        <p className="nd-label">{label}</p>
        <h2 id={id} className="gd-h2">
          {title}
        </h2>
      </div>
      {children}
    </section>
  );
}

export function GuidePage() {
  useDocumentTitle("Product guide");
  const isAdmin = useSession().data?.user.role === "admin";

  return (
    <article className="gd">
      <header className="gd-head">
        <p className="nd-label">Product guide</p>
        <h1 className="gd-h1">How hive works</h1>
        <p className="gd-lead">
          hive joins a few trusted laptops into one compute fabric. You submit a job to the fabric, not to a machine, and
          hive decides where and how it runs.
        </p>
      </header>

      <Section id="gd-question" label="01 · The one question" title="Every job gets one answer">
        <p className="gd-q">
          Given this job and the laptops available right now, what is the best practical way to run it?
        </p>
        <p className="gd-p">
          A small agent on each laptop reports its hardware (CPU, RAM, GPU, VRAM), how busy it is and how fast the network
          is. One planner uses that to choose the way the job runs, which laptops take part and how much work each one
          gets, and it can always say why.
        </p>
      </Section>

      <Section id="gd-ways" label="02 · Ways to run" title="Five ways a job can run">
        <div className="gd-ways">
          <span aria-hidden="true" />
          <p className="gd-axis nd-label" aria-hidden="true">
            One laptop
          </p>
          <p className="gd-axis nd-label" aria-hidden="true">
            Many laptops
          </p>
          {WAYS.map((way, i) => (
            <WayCell key={way.mode} way={way} rowLabel={i % 2 === 0 ? (i === 0 ? "Task is sent away" : "Program stays on your laptop") : null} />
          ))}
        </div>
        <div className="gd-local nd-hair-t">
          <LocalFigure />
          <p className="gd-p">
            <span className="gd-local-name">Local</span>
            <span className="gd-built nd-label">Built</span>
            The job runs on your own laptop. It is always the baseline: for small jobs, sending data over the network costs
            more time than it saves.
          </p>
        </div>
      </Section>

      <Section id="gd-flow" label="03 · Flow" title="How a job flows">
        <ol className="gd-steps">
          {STEPS.map((step, i) => (
            <li key={step.title}>
              <span className="gd-step-n nd-label">{String(i + 1).padStart(2, "0")}</span>
              <h3 className="gd-h3">{step.title}</h3>
              <p className="gd-small">{step.text}</p>
            </li>
          ))}
        </ol>
      </Section>

      <Section id="gd-pages" label="04 · The dashboard" title="The pages">
        <dl className="gd-list">
          {PAGES.map((page) => (
            <div key={page.to} className="gd-row nd-hair-t">
              <dt>
                {page.adminOnly === true && !isAdmin ? (
                  <span className="gd-page">{page.name}</span>
                ) : (
                  <Link to={page.to} className="gd-page gd-page-link">
                    {page.name}
                    <span aria-hidden="true" className="gd-arrow">
                      →
                    </span>
                  </Link>
                )}
              </dt>
              <dd>
                <p className="gd-p">{page.text}</p>
                {page.sub && (
                  <ul className="gd-sub">
                    {page.sub.map((s) => (
                      <li key={s.name}>
                        <span className="gd-sub-name">{s.name}</span> {s.text}
                      </li>
                    ))}
                  </ul>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </Section>

      <Section id="gd-terms" label="05 · Glossary" title="Words you'll see">
        <dl className="gd-terms">
          {TERMS.map((t, i) => (
            <div key={i}>
              <dt className="gd-term">{t.term}</dt>
              <dd className="gd-small">{t.text}</dd>
            </div>
          ))}
        </dl>
      </Section>
    </article>
  );
}

function WayCell({ way, rowLabel }: { way: Way; rowLabel: string | null }) {
  return (
    <>
      {rowLabel !== null && (
        <p className="gd-axis gd-axis-row nd-label" aria-hidden="true">
          {rowLabel}
        </p>
      )}
      <div className="gd-way nd-hair-t">
        {way.figure}
        <p className="gd-way-meta">
          <span className="nd-label">{way.mode}</span>
          <span className={way.built ? "gd-built nd-label" : "gd-coming nd-label"}>{way.built ? "Built" : "Coming"}</span>
        </p>
        <h3 className="gd-h3">
          {way.name}
          {way.shown !== undefined && <span className="gd-shown">shown as “{way.shown}”</span>}
        </h3>
        <p className="gd-way-axis nd-label">{way.axis}</p>
        <p className="gd-small">{way.text}</p>
      </div>
    </>
  );
}
