# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md (including section 0, the scope change), then
> docs/adr/0012-scope-ship-mode3-complete.md, then docs/STATUS.md, then the current
> version in docs/DEVELOPMENT_ROADMAP.md, and continue the work exactly where it stopped.
> ```
>
> Order of truth: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → this file`. If this file
> disagrees with the roadmap, the roadmap wins and this file is stale — fix it.

> # 🚨 THE PROJECT PLAN CHANGED ON 2026-09-20 — DO NOT SKIP SECTION 0
>
> **If your mental model of this project is "build all four execution modes", it is out of
> date.** The plan is now **Mode 3 only, complete and production-grade**, with Mode 1 as its
> foundation. **Mode 2 and Mode 4 are deferred.** This came from a budget the team had never
> stated before (one Claude account, ~10 weeks). See **section 0** below and
> **[ADR-0012](adr/0012-scope-ship-mode3-complete.md)**.
>
> **You are expected to review this decision, not just obey it.** It is **Proposed**, not
> Accepted — the other three team members have not approved it yet, and `CLAUDE.md` and
> `PROJECT_BRIEF.md` still describe the old four-mode plan on purpose. The ADR carries the
> throughput measurements and the reasoning; if you think the arithmetic or the trade is wrong,
> **say so to the team before building on it.** Do not silently re-expand the scope either.

**Last updated:** 2026-09-21 (**V0.3 G4 in progress** — §5d brief written, 0.3.3/0.3.5/0.3.11 `[~]`, implementer running; G1–G3 closed; V0.2 waits on 🧑 lab runs) · **Updated by:** Lead session (account 2)

---

## 0. SCOPE CHANGED 2026-09-20 — read before anything else

The team stated the real budget for the first time: **one Claude account, ~10 weeks, ~9 sessions/week ≈ 90
sessions.** The old "no deadline, never cut scope for time" rule is **dead**.
**[ADR-0012](adr/0012-scope-ship-mode3-complete.md) (Proposed — needs the other three team members):**

- **Ship Mode 3 (Task Splitting) complete and production-grade.** Mode 1 ships with it as its foundation.
- **Keep the AI in full** — V0.9 telemetry, V0.14 planner v2, V0.15 ML planner. An earlier draft cut these and
  the team rejected it: "AI has a real optimization role" is a fixed pillar.
- **Keep V0.7 security and V0.8 dashboard in full**, plus V0.17 and V1.0. **Nothing is trimmed at 10 weeks.**
- **Defer Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16)** — marked DEFERRED in the roadmap, kept in full as the
  future-work record. Revisit **V0.11 first** if margin survives; Mode 4 extends Mode 2.
- **V0.9 moves before V0.10** so ML training data accumulates during Mode 3 development.
- **0.2.8 (S4) and 0.2.9 (S5) are deferred with Mode 2.** ADR-0011 is off the critical path (torch still needed
  for GPU-backed Mode 3 chunks, but it blocks nothing now).
- **129 tasks against 140–180 capacity → target ~1.5 tasks/session.** Degradation order if the budget shrinks is
  fixed in the roadmap's "Scope & budget" section: V1.0 → V0.17 → thin V0.8 → V0.5 polish. **Never V0.10, V0.15,
  V0.9.**
- **Breadth-vs-depth resolved:** the college rewards *working quality*, not a count of features. Depth is correct,
  so the quality rules (tester + reviewer, evidence before `[x]`, real multi-machine tests) all stay.

**Token efficiency is half of why this fits** and is now a working rule, not advice: one *coherent group* per
implementer run (not one task) · front-load every brief with the standing "no confidently-wrong output" checklist
so reviews pass in 1–2 rounds · the Lead does doc edits and small mechanical fixes directly with **no subagent** ·
run the ~6-minute component suite **once per task group**, not once per agent. Evidence: a ~30-line `_harness`
change cost **54 k tokens** as a subagent; 0.2.6 (S2) needed **7** agent runs; S6 passed its implementer round
**first time** on a front-loaded brief.

### What a fresh session must CHECK about this plan change (do this before building)

1. **Is ADR-0012 still `Proposed`?** If the team has accepted it, change the status, amend
   `CLAUDE.md` ("No deadline" clause) and `PROJECT_BRIEF.md` §33 + §40, and delete this
   instruction. If it has been **rejected**, the roadmap's DEFERRED banners and the
   "Scope & budget" section must be reverted — do not leave the repo half-way.
2. **Does the hardware still match?** The whole plan assumes L2 ≈ RTX 3050, L3 ≈ RTX 2050,
   **L4 no GPU** — *team-stated, never measured*. If `lab/inventory.py` results have landed and
   contradict that, re-open the ADR: fewer GPUs weakens Mode 3's speedup story, and more/larger
   GPUs would make Mode 2 cheaper to justify.
3. **Is the throughput assumption holding?** The plan needs **~1.5 tasks/session**. If the last
   two weeks ran below ~1 task/session, the plan is slipping: cut in the fixed order in the
   roadmap's "Scope & budget" section (V1.0 → V0.17 → thin V0.8 → V0.5 polish) and **never**
   V0.10, V0.15 or V0.9.
4. **Has anyone started Mode 2 or Mode 4 work by accident?** V0.11/V0.12/V0.13/V0.16 are
   DEFERRED. If a task from those versions is in progress, stop and ask the team.
5. **Is telemetry (V0.9) still scheduled before Mode 3 (V0.10)?** It was moved deliberately: the
   ML planner needs thousands of real runs to train on, and built late there is no data at the
   end. If someone has reordered it back, that is a regression in the plan, not a detail.

Docs already updated: `docs/adr/0012-*`, `DEVELOPMENT_ROADMAP.md` (header, Scope & budget section, DEFERRED
banners, V0.14 re-scope, dependency graph, design change log), `PROJECT_SUMMARY.md`, `docs/LAB.md` §1.
**Not yet updated: `CLAUDE.md` (still says "No deadline") and `PROJECT_BRIEF.md` §33/§40** — both need team
approval of ADR-0012 first.

---

## 1. Where the project is

- **Version:** V0.2 (lab setup & feasibility validation). V0.1 is code-complete; its last two
  tasks (0.1.4, 0.1.10) only await the **first real GitHub CI run**, which needs a human push.
- **All four V0.2 spikes are code-complete with reviewer APPROVE** and committed:
  0.2.5 S1 (`ef065ac`), **0.2.6 S2 (`633d474`, closed 2026-09-20)**, 0.2.7 S3 (`81d620f`),
  0.2.11 S7 (`8072b9f`). 0.2.2 `lab/inventory.py` (`75d8df5`). ADR-0011 written, Proposed.
  Every one of these is `[~]` only because it awaits 🧑 runs on real laptops.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]`
  because `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

**V0.3 G4 (0.3.3 hardware discovery + 0.3.5 resource monitor + 0.3.11 snapshots) — fix round DONE and
Lead-verified; an independent tester round and the reviewer are running in parallel.**

Timeline: implementer `5fa761b`→`eac66e6` · tester FAIL `9b94774` (R1 critical, H1 major) · fix round **`ac20ea3`**
(preceded by `743b178`, work-in-progress recovered after the fix agent was cut off mid-run — it never wrote its
handoff note, so the record below is the Lead's, from reading the diff).

**R1 fixed:** dedupe is on `ResourceUpdate.sequence` alone with the cursor reset armed by
`Reconcile.replay_from_sequence` via a new `ResourceSink.begin_session`; a lower `session_epoch` (a superseded zombie
stream) is rejected and cannot move the cursor. **H1 fixed:** new `agent/hardware/deadline.py` runs a blocking probe
on its own daemon thread and *abandons* it on expiry — `asyncio.to_thread` was rejected because its workers are
joined by `shutdown_default_executor()` and the `concurrent.futures` atexit hook, so one wedged NVML call would have
hung process exit; one slot, so a permanently wedged driver leaks one thread, not one per sample, and discovery
degrades to `basic_hardware()` so the node still enrolls. **L1 fixed:** `SHAPE_CAPABILITY` derives from
`hello.CAPABILITIES_V03`. The Lead also fixed one stale call site the interrupted run left behind
(`discover_hardware` gained a `guard` parameter).

**Lead verification, independent of the agents' own tests** (scripts in the session scratchpad, not the repo):
replay rejected · restart accepted · same-process reconnect accepted · zombie epoch rejected · bucket `samples == 1`
· deadline bounded at 0.301 s against a 5 s wedge · one leaked daemon thread · `asyncio.run()` returns without
waiting for it. Gates at `ac20ea3`: ruff + format clean, mypy **78** files, lint-imports 7 kept / 0 broken,
gen_protos clean, unit **2573 passed / 2 skipped / 2 xfailed**, component agent+store+controller **121 passed**.

**If this session died here:** wait for (or re-run) the tester and reviewer, batch whatever they find into one
implementer round, then mark 0.3.3 / 0.3.5 / 0.3.11 `[x]` with evidence and write the **G5** brief (0.3.12 REST +
CLI, 0.3.13 service units). **Carry into G6:** the tester could not build a "controller stays up, the client's TCP
drops and reconnects" component test — the existing harness restarts the controller, which destroys the in-memory
tracker along with it. That harness gap is G6 fault-injection work and it is why R1 survived the first round.

### 2d. G4 cleanup + tester round 3 — ISO-1 (the fix that introduced the opposite bug)

**Cleanup round done** (`1a46004` → `89ce269`, 6 commits): D-EVICT-1 + reviewer NB-1/3/4/5/6/7/8/9/10. Dedupe state
moved to a `_Cursor` in its own LRU (`controller.resources.max_cursor_nodes`, default 16 384, clamped ≥
`max_tracked_nodes`), so ring pressure no longer drops cursors. **Lead-verified at HEAD:** all five R1 cases still
correct; and the NB-1 guard genuinely fails when `await asyncio.sleep(0)` is inserted before
`_reconcile_and_replay()` and passes when reverted — the ordering invariant is enforced, not merely documented.
Gates: mypy 78, lint-imports 7/0, unit **2606**, component **124**.

**Tester round 3: FAIL — ISO-1 (critical).** Worth understanding, not just fixing: **the logic written to fix NB-6
("a poison row blocks history") introduced its inverse ("an outage drops history").** `_isolate`'s guard
`if not ok and not self._isolating: return` only protects the *first* episode, because `_isolating` is set as
`bool(keep)` and persists across ticks. After any earlier episode latched it, a later full outage falls through to
the blame branch and can drop a row that was never individually proven poison twice. Pinned by
`tests/unit/controller/test_snapshot_flusher_isolation_outage_defect.py`.

**The tension the fix must respect** (a naive latch removal re-opens NB-6): if the *only* pending row is poison,
nothing ever gets through, `ok` is always empty, and "never blame when `ok` is empty" means that row blocks every
flush forever. Both must hold: **(a)** a full outage never blames or drops a row, whatever happened earlier;
**(b)** a lone poison row is still dropped after genuine individual failures. Lead's suggested signal: when a pass
ends with `ok` empty, make one explicit liveness check and blame only if the database answers — a fact established
in the current pass, replacing a stale latch.

Tester **PASSED** everything else it attacked, by test or by traced inspection: the cursor LRU (an armed node is
`move_to_end`-ed on every touch, so it stays MRU — the D-EVICT-1 race now needs 16 384 distinct nodes of churn, and
is counted and logged), NB-3's upsert guard, NB-4's key cap, NB-9's epoch high-water. It **confirmed the import
cycle** behind the deferred `MAX_GPUS`/`MAX_DISKS` import by reproducing the real `ImportError`
(`resources` → `session/__init__` → `manager` → `registry` → `resources`). Minor follow-up folded into the ISO-1
round: `_row_failures` can leak an orphaned entry for a row evicted by `max_pending_rows` before a second probe.

### 2c. G4 tester round 2 (FAIL, 2026-09-21) + the batched cleanup round

**Tester FAIL at `ac20ea3` with exactly one defect; everything else PASS.** Commits `0af3f49`, `11c2235`,
`1a46004` (22 new unit tests + 1 component). It **ruled out** the hazards the Lead and reviewer were most worried
about, with tests rather than argument: an abandoned `DeadlineThread` finishing late cannot corrupt a later call's
result (each call keeps its own `box`/`done`), `begin_session` arming does not leak across nodes, a zombie-epoch
report arriving while a reset is armed is fenced and the arm survives intact, a `Reconcile` arriving after the
session's first report does not retroactively reopen a cursor, and a wedged discovery genuinely still enrols the
node with `basic_hardware()` inside the budget. No flakiness (deadline tests 5×, component wedge 3×).

**D-EVICT-1 — `controller/resources.py` `_ring_for`.** `max_tracked_nodes` LRU eviction and the `begin_session`
arm/Reconcile handshake are unaware of each other: if a node reconnects (reset armed) but its `Reconcile` has not
arrived, and other nodes push it out under the node cap, eviction pops its `_cursor`, `_epoch` **and**
`_reset_armed` — so the later replay has no cursor to be rejected against and is recorded twice. **The double-count
R1 closed, reopened through the eviction path.** Pinned by a deliberately failing test:
`tests/unit/controller/test_resource_tracker_begin_session_g4_fix_round.py::test_eviction_while_a_reconnect_is_armed_drops_the_kept_cursor`.
Lead's fix direction: the memory bound exists for the **rings** (150 samples), not the cursors (two ints and a
flag) — evict rings under `max_tracked_nodes`, keep cursor state in its own bounded LRU with a far larger cap and a
counter when *it* evicts.

**Cleanup round dispatched** (one implementer run batching D-EVICT-1 + reviewer NB-1, NB-3, NB-4, NB-5, NB-6, NB-7,
NB-8, NB-9, NB-10 — the ten items detailed in §2b). **If this session died here:** the brief is reconstructable
from §2b's list plus D-EVICT-1 above; after it, re-run the tester on the changed areas, then mark
0.3.3 / 0.3.5 / 0.3.11 `[x]` with evidence and write the **G5** brief.

### ⚠ Process rule learned the hard way (2026-09-21)

**Never run `git add -A` while a subagent is running.** The Lead did, and swept a tester file that was mid-write
into an unrelated docs commit (`26eb91a` carries `test_agent_discovery_wedge_g4_fix_round.py`). Nothing was lost and
the tree is consistent, but the commit boundaries now lie about who wrote what. Stage explicit paths
(`git add docs/...`) whenever an agent may be writing, or commit only after it has finished.

### 2b. G4 reviewer outcome (APPROVE, 2026-09-21) — cleanup round to dispatch

**APPROVE at `ac20ea3`, conditional on the in-flight tester round returning PASS. No blocking findings.** The
reviewer walked the R1 state machine against `client.py` / `manager.py` and ruled it *correct, not merely
correct-looking* (ordered stream ⇒ "received" is always a prefix of the ring, so no silent loss; fresh process sends
no `Reconcile` and the armed reset clears the cursor; a superseded stream's reader is cancelled and re-checks
`live.ended` before dispatch). It ruled `deadline.py` sound (`box` is per-call and closure-captured, so an abandoned
thread can never write into a later call's slot) and re-verified the 0.301 s bound itself. All six §2a deviations
**accepted**. Layering held (`manager.py` grew 84 lines, no downsampler, imports only the Protocol). No ADR needed —
D9–D12 belong in the brief. No architecture drift.

**Fix in the cleanup round** (Lead's selection — each is cheap and several are silent data corruption):
1. **NB-1 `client.py:478-506` — the R1 fix rests on an undocumented, untested ordering invariant.** ONLINE at `:478`
   reaches `_reconcile_and_replay()` at `:506` with **no suspension point between**, so the Reconcile takes
   `_write_lock` before a monitor tick woken by the ONLINE transition can. Add one `await` in that window and a fresh
   report overtakes the Reconcile: the armed reset is consumed by the *new* report, the cursor jumps forward, and the
   replayed reports the controller never received are silently dropped. **Needs a comment at `:504` forbidding an
   `await` there and a component test asserting Reconcile is the first `AgentMessage` of every reconnect.**
2. **NB-3 `registry.py:491-503` — the snapshot upsert overwrites unconditionally.** After a controller restart a
   replayed report can re-accumulate a bucket from a partial sample set and `DO UPDATE` replaces the good row
   (`samples` 30 → 2). Fix: `where=ResourceSnapshot.samples <= excluded.samples`.
3. **NB-4 `resources.py:226-259` — per-bucket key cardinality is unbounded.** D12 caps 16 GPUs / 16 disks *per
   message*, but the accumulator dicts are keyed across every sample in the bucket, so an agent varying the mount
   string yields ~480 JSONB keys in one row. Cap at `MAX_GPUS`/`MAX_DISKS` with a dropped counter.
4. **NB-6 `snapshots.py:103-108` — one poison row blocks the whole batch** (single statement), so a row whose `node`
   is missing fails every later flush until the 5 000-row cap evicts it — ~20 h of lost history at 4 nodes. This is
   **worse than the logged "retried then dropped" follow-up.** Retry row-by-row on `RegistryError`, or drop after two
   failures with a counter.
5. **NB-5 `snapshots.py:13` overclaims** "never raises out of `run()`" — `flush_once` catches only `MeshForgeError`,
   so a `TypeError` escapes to `_on_task_done` and stops the controller. Broaden the guard or soften the docstring.
6. **NB-10 `server.py:288`** — `flush_once` clears `_pending` before the await, so cancelling mid-upsert loses a
   batch. Clear only after the write succeeds.
7. **NB-9 `resources.py:463`** — `begin_session` does not raise the epoch high-water mark, so the fence is inert in
   the window right after a reconnect. Pass `session_epoch` and `max()` it in (defence in depth).
8. **NB-7 `agent/hardware/limits.py:80`** — `"v0.3"` is hardcoded a **third** time in `clip_capabilities`; L1 only
   collapsed two. Import `CAPABILITIES_V03[0]`.
9. **NB-8 `agent/hardware/limits.py:8` and `controller/session/limits.py:124` name the wrong test file** — they cite
   `test_session_limits`; the parity is actually pinned by `tests/unit/controller/test_resource_limits.py:189`. The
   reviewer nearly filed a blocking "claimed coverage does not exist" on this.

**Escalate to the team (not a reviewer call) — NB-2:** on a laptop where NVML is missing but `nvidia-smi` works, the
static inventory lists the GPU but **every** `ResourceUpdate` omits GPU util and VRAM, permanently. V0.9 telemetry
and the V0.15 ML planner would then train on a node that looks like it has a GPU and never uses it. Refusing a 2 s
subprocess was right; the option worth costing is a low-cadence `nvidia-smi` poll (~30 s, interpolated) behind a
setting.

**Defer:** NB-11 (`ProbeOutcome.reason`'s `ERROR` branch interpolates an exception whose `OSError` carries a path —
sanitise before `HardwareFacts` is served over REST) → **G5**. NB-12 (`manager.py` now ~913 lines, still on the
extraction list) → **V0.4**.

### 2a. G4 declared deviations the reviewer must rule on

1. `ResourceSink.record(node_id, session_epoch, update, now)` — §5d named `record(node_id, update, now)`. Claim: after
   a reconnect a replay arrives on a new session, so `(session_epoch, sequence)` is the only correct duplicate key;
   sequence alone breaks on an agent restart. **🔴 CONFIRMED DEFECT — the Lead reproduced it 2026-09-21.** Half the
   claim is right (an agent *restart* does reset `_report_sequence` to 0, so a bare sequence cursor would reject the
   new process's reports), but the chosen key breaks the case §5d explicitly required — *"a duplicate (replayed)
   update is not double-counted"*. `next_epoch` is bumped **per connection attempt**, not per process
   (`client.py:339`), while `_report_sequence` is per process and never reset (`client.py:204`). So a report sent as
   `(epoch 5, seq 100)` whose Ack was lost is replayed after the reconnect as `(epoch 6, seq 100)`; lexicographically
   that is *greater* than the cursor, so it is accepted again. Reproduced directly against `ResourceTracker`:

   ```
   record(node, epoch=5, seq=100) -> True
   record(node, epoch=6, seq=100) -> True      # the replay; expected False
   recorded: 2  duplicates: 0                   # expected recorded 1, duplicates 1
   ```

   The sample lands in the live ring twice **and** is added to the downsampler twice, so `resource_snapshot.samples`
   and every mean are wrong after any lossy reconnect — silently, and exactly on the flaky links this project is
   built for. The implementer's component test that claims "replay not double-counted" therefore tests the wrong
   thing: it must replay *with* the epoch bump that a real reconnect performs.

   **Fix direction (implementer to verify, no proto change needed):** dedupe on `sequence` alone — it is already
   per-process monotonic — and reset the cursor using information that is already on the wire:
   `Reconcile.replay_from_sequence`. A same-process reconnect that has buffered reports sends the oldest buffered
   sequence (> 0), so the controller **keeps** its cursor and rejects the replayed duplicates; a fresh process sends
   0 (G3's reviewer fix made the empty ring send 0), so the controller **resets** the cursor and accepts the restarted
   agent's sequence 1. A same-process reconnect with an empty ring also sends 0, where resetting is harmless because
   the next sequence is higher anyway. This needs a `begin_session(node_id, replay_from_sequence)` on the sink and the
   manager's `reconcile` branch to call it.
2. Live GPU *usage* is NVML-only. If NVML is unavailable but `nvidia-smi` works, the static inventory still lists the
   GPUs but per-sample usage is omitted with a recorded reason (a subprocess every 2 s was judged unacceptable).
   **This is a real capability gap on any machine where NVML is missing — worth the team's eye, not just the reviewer's.**
3. `torch` tag from `find_spec` (no import); only `torch-cuda` costs a child process, probed last within the budget.
4. Extra `controller.resources.max_open_buckets` + a bounded pending-row list in the flusher (not in §5d; defensive).
5. Migration `0003` also adds `SessionEndReason.INTERNAL_ERROR` and the manager uses it for a reader crash — this one
   was explicitly authorised by §5d.
6. No proto change was needed (as §5d predicted).

**Lead finding while the tester ran (hand to the reviewer, do not patch mid-tester-run):** the
`"v0.3"` Hello-shape marker is now defined **twice** — `agent/session/hello.py::CAPABILITIES_V03`
and `agent/hardware/summary.py::SHAPE_CAPABILITY`. Two constants for one wire value is exactly the
silent drift `controller/session/limits.py`'s docstring warns about. The fix direction is
`hardware/summary.py` importing it from `hello.py`, **not** the reverse: `hello.py` deliberately
imports no agent-extra package so `meshforge-agent --version` works on a base install.

Implementer follow-ups logged for later: `HardwareFacts` is built but exposed nowhere (G5's REST should serve it);
`HardwareProvider.refresh()` has no caller until G7/0.3.4; snapshot rows whose `node` row is missing are retried then
dropped (FK); small vfat partitions such as `/boot/efi` pass the fixed-filesystem filter; the agent's `capabilities`
no longer match G3's `["v0.3"]` shape, and `tests/component/agent/test_agent_session.py` still asserts the fallback
shape for a client built without `hardware=`; bare `uv run mypy` (src+tests+scripts+lab) has **54 pre-existing**
errors at HEAD~ — the gate is `mypy src`, and widening it is a team call.

- **G1** (`da27ff1` → `0acf7eb`): proto, identity (ADR-0013), liveness logic. 0.3.2 `[~]` until a 🧑 Windows unit run.
- **G2** (`bd416eb` → `a1014d5`): controller session server, registry, leases, RTT, one process. 0.3.7 `[~]` for lab evidence.
  Dev-DB notice: migration `0002_nodes` was amended in place on 2026-09-20 — drop or re-migrate a dev DB from that day.
- **G3** (`a934621` → `0f55af0`): agent runtime + discovery. 0.3.8 / 0.3.9 `[x]`.

Pattern that worked: every implementer/tester/reviewer run on a front-loaded brief; two implementer runs were cut by
usage limits and recovered from a `wip:` commit — **always commit partial work before relaunching**. Follow-ups from
each review are in the roadmap design change log (rows "V0.3 G1/G2/G3 review follow-ups").

**G4 (hardware + resources + snapshots, 0.3.3 + 0.3.5 + 0.3.11) — brief §5d is written.** Real API it builds on: `AgentSessionClient.send_report(payload_name, payload)` with
`REPORT_PAYLOADS` (reports are ring-buffered and replayed; `ResourceUpdate.sequence` is stamped by the client);
`Hello.hardware` = `HardwareSummary` (currently basics only, `capabilities=["v0.3"]`); controller stores
`Hello.hardware` as JSON on the `node` row (`Registry.upsert_node_on_hello`) — G4 must cap its size (G2 follow-up);
`lab/_inventory/probes.py` has the probe logic to lift (never import `lab/` from `src/`); `lab/spikes/_s7/` has the
score kernels for G7. Gates at handoff: ruff / mypy (60 files) / lint-imports / gen_protos clean, unit **2437**,
component agent **37**, store+controller+agent **~92**.

**Documented loss in 0.2.13, not a bug:** hand-edits *inside* a generated region (e.g. filling
"speed: TBD" or the power-mode cell in the host table) are still overwritten. Recorded in
`lab/README.md`. Making those cells human is a design change worth an ADR.

**0.2.10 (S6) closed agent-side 2026-09-20** — tester PASS (no defects, 16 adversarial tests),
reviewer APPROVE (no blocking findings). Commits `f7b094c` `f3364b8` `bfce2fa` `fb9376a`
`c97f013`. Real Laptop 1 run: **the GPU is visible inside a container** (nvidia-smi 361 ms), cold
pull 9.7 s, venv cold 3.56 s vs warm **0.10 s** (V0.5 criterion 5 wants ≤ 3 s job-start overhead,
so the warm path has plenty of room). **Stays `[~]`:** `docker-gpu-torch` was skipped as typed
`insufficient_disk` (5.8 GB free, image ~3.7 GB), so `torch-cuda-in-container` is **TBD on every
laptop**, ARCHITECTURE Q4 is unanswered, and Windows/Docker Desktop has **zero** evidence.

**Remaining non-blocking follow-ups from the 0.2.10 review** (logged in the roadmap design change
log, none started): cross-check `summary.n == len(samples)` and render INVALID on mismatch (a
better honesty check than any upper bound on `n`) · `TEMPLATE.md:34` hardcodes "n < 5" while all
code uses `MIN_VALID_REPS` · `_s6/run.py` `_cleanup` removes **every** `s6-` container, so two
concurrent S6 runs on one host would kill each other's.

**After 0.2.13:** all V0.2 agent-side work is done. **0.2.12 must wait for real lab data** — never
run it on loopback numbers. The next real work is **V0.3, the first version of the new plan**, and
it is blocked on the 🧑 inventory of laptops 2–4. The S3-review follow-up (promote `_s1.protocol`
into `_harness/`) is **done**: `_harness/protocol.py` holds framing + LAN address rules;
`test_harness_protocol_boundary.py` fails if any non-S1 spike imports `spikes._s1` again.

**Harness rules every spike must respect:** framing / address rules come from `_harness/protocol.py`, never from another spike's package · envelope `link` may be `None` (n/a); `load_dir` skip
reasons carry only `path.name`; `envelope.sanitize_text()` on any extra markdown; `report.cell()`
for *any* interpolated free text in a markdown table cell; `_harness/report.py` renders a
malformed case entry as one INVALID (n=0) row + note; report writes go through `write_report()`.

**File ownership while several agents run:** each spike owns only its own paths
(`lab/spikes/<sN>_*.py`, `lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`).
`_harness/**` is shared — one agent at a time.

## 3. Blocked on humans (nothing agent-side unblocks these)

0. **🔴 Run `uv run python lab/inventory.py` on laptops 2, 3 and 4** — now the top blocker. **V0.3 cannot start
   without it.** It confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050,
   **L4 no GPU**), fills `docs/LAB.md` §1, and settles ADR-0011. If L4 really has no GPU that is *good* — it is what
   gives the planner a real decision to make.
0b. **Team review of [ADR-0013](adr/0013-node-identity-storage.md) and the ADR-0002 Updates (2026-09-20)** — both Proposed; G1 builds
   on them, so objections are cheapest before G1 starts (key type P-256, `node_id` not key-derived, epoch fencing rules).
0a. **Team approval of [ADR-0012](adr/0012-scope-ship-mode3-complete.md)** — it changes an item `PROJECT_BRIEF` §33
   calls "relatively fixed", so it stays Proposed, and `CLAUDE.md` + the brief stay un-amended, until the other
   three agree.
1. **GitHub push + CI proof** — closes V0.1 (criteria 1–2) and lets laptops 2–4 `git pull`.
   Steps are in `docs/STATUS.md` → Next Recommended Task. **Agents never push.**
1a. **Run the S2 spike on two laptops** — Windows↔Linux, **both directions**, Wi-Fi **and**
   Ethernet: `lab/spikes/S2.md` §3 (server-side firewall TCP 47202–47203 only; the client
   side must have **no** inbound rule and pass `--no-inbound-rule`; also do the Windows
   second-`serve` check in §3.2). Paste back both JSON files + stderr per direction.
   Until then ADR-0002/ADR-0009 have no real cross-OS evidence.
1b. **Run the S3 spike on two laptops**, Wi-Fi **and** Ethernet — procedure in
   `lab/spikes/S3.md` §2 (firewall TCP 47301–47303; one pair at a time; never together with
   S1). Until then ADR-0003/ADR-0005 have no real-link evidence and 0.2.12 cannot start.
   The loopback numbers in `S3.md` §4 bound the tool, **not** the link — never quote them
   as evidence about the network.
2. **Team decision on [ADR-0011](adr/0011-torch-index-per-platform.md)** (torch from the cu126 index).
   **No longer urgent:** S4 (0.2.8) and S5 (0.2.9) are deferred with Mode 2 (ADR-0012). torch is still needed for
   GPU-backed Mode 3 chunks and to close 0.2.1. Note the provisional inventory says all three GPUs are Ampere
   (CC 8.6), which is exactly the fact ADR-0011's cu126-vs-cu130 choice turns on.
3. **Run `lab/inventory.py` on all 4 laptops** (commands in `lab/README.md`) — the results
   decide cu126 vs cu130 and fill `docs/LAB.md` §1.
4. **Buy the gigabit switch, set DHCP reservations, set Windows profile to Private** (0.2.4);
   S1 needs both Wi-Fi and Ethernet matrices.

## 4. Traps a fresh session must know

- **Never mark `[x]` without evidence** (test output, CI link, or `lab/reports/…`). Only the
  Lead marks it, after tester PASS **and** reviewer APPROVE.
- **Tests must not depend on this machine's state.** The driver fix broke two tests that had
  "the driver is broken" baked in. Assert behaviour, not hardware.
- **Agents commit locally, one commit per task; humans push.** Never run `git push`.
- **`uv run` re-syncs the environment**; don't edit `pyproject.toml`/`uv.lock` while another
  agent is running tests.
- Lab scripts are stdlib-first: they must work after a plain `uv sync` on native Windows too.
- torch is **not** installed here; GPU paths must record a clean "torch not installed" error.

## 5. Account rotation (three Pro accounts, same machine, same repo)

The accounts share this working directory, so **the repository is the shared memory**. Before
switching, and ideally after every finished step:

1. Update this file (sections 1–3) and `docs/STATUS.md`.
2. Commit work in progress (`wip:` prefix is fine) so the next account sees committed truth and
   can read `git log`.
3. On the new account, paste the cold-start prompt at the top of this file.

If a limit hits mid-task with no warning, the next account recovers from this file plus
`git status` / `git log`; that is exactly why sections 2 and 4 exist.
