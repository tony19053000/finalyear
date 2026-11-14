# ADR-0025: Windows job processes run with HighQoS (power throttling off)

> **Status:** Proposed (2026-11-14) — lab-proven root cause (G13), fix D461 · **Deciders:** team + Lead agent

## Context
Lab on v0.17.4 found Windows jobs 3–25x slower than the same hardware by hand. On L4 (i5-12450H, hybrid P+E
cores) the same CPU CNN took 48 s by hand, 78 s as SYSTEM on High performance and 164 s on Balanced; with
HighQoS set it took 48 s on both plans. One GPU chunk (16 CUDA trainings): 28.5 s by hand, 45–77 s as SYSTEM,
30 s as SYSTEM + HighQoS. Every job is a child of the agent **service** (session 0, no window) with no power
throttling QoS, so Windows' power manager (EcoQoS heuristics) treats it as background work: efficiency cores
and no clock boost. Priority class (NORMAL vs BELOW_NORMAL) alone changed nothing. Defender, numpy, env,
staging and the CPU cap were ruled out (L4 chat 2026-11-14 T6, T13–T17).

## Decision
`agent/process.spawn()` — the one path for jobs, split lane chunks and env builds — sets **HighQoS** on the
child while it is still suspended (after the job object, before `NtResumeProcess`):
`SetProcessInformation(ProcessPowerThrottling, PROCESS_POWER_THROTTLING_STATE{Version=1,
ControlMask=EXECUTION_SPEED, StateMask=0})`. The agent also sets it on its own process at start, so anything
it starts outside `spawn()` inherits it. Processes a job starts inherit the setting (checked on Windows CI
with a grandchild). One structured line `agent.process.qos` per spawn (`applied`, Win32 `error`); a failure
is logged and the job runs anyway (only slower). HANDLE arguments/results are `c_void_p` (64-bit trap).
The owner's controls are unchanged: `BELOW_NORMAL` priority and the job object's CPU-rate cap still apply.

## Alternatives
1. **Run jobs in an interactive user session** (task scheduler / `CreateProcessAsUser` into the console
   session): gets foreground treatment but needs a logged-in user, breaks headless laptops and widens the
   security surface. Rejected.
2. **Tell owners to use the High performance plan:** still 78 s vs 48 s, and a fabric must not require a
   power-plan change. Rejected.
3. **Raise priority to NORMAL/ABOVE_NORMAL:** measured no effect, and it hurts the owner. Rejected.
4. **Pin to P-cores via affinity / CPU sets:** fragile across CPUs, fights the scheduler. Rejected.

## Trade-offs
Jobs may now use P-cores at full boost: more heat/battery while a job runs. That is the purpose of lending
the laptop, and the owner keeps the CPU cap, BELOW_NORMAL priority, owner-activity pause and policy limits.

## Consequences
- Windows CPU and GPU jobs run near hand speed (expected L4: CNN ~48–55 s, GPU chunk ~30 s).
- New log events `agent.process.qos` / `agent.process.qos_self`; Windows CI checks child + grandchild state.
- Lab re-run of the slow cases on L4 and L2 decides acceptance.
