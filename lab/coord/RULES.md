# Lab agent rules (D426)

You are the **lab agent** for ONE laptop (L1, L2, L3 or L4) in the MeshForge lab. The Lead session (on L1's
main repo) plans the work; you run it on your laptop and report. A human from the team sits next to you.

## Your loop

1. `git fetch origin && git checkout lab/<Ln>` (create it from `origin/main` the first time:
   `git checkout -b lab/<Ln> origin/main`), then `git merge origin/main` to get the Lead's newest TODO.
2. Read `lab/coord/<Ln>/TODO.md`. Do the steps **in order**. A step marked `WAIT` needs another laptop
   first: check `git fetch` + the other laptop's `lab/coord/<Lx>/REPORT.md` on `origin/lab/<Lx>`, or ask
   the human.
3. For every step, append to `lab/coord/<Ln>/REPORT.md` (format below), and save raw outputs where the step
   says (normally `lab/reports/...`).
4. After each finished step: `git add lab/ && git commit -m "lab(<Ln>): <step id> <ok|FAIL>" && git push
   origin lab/<Ln>`. Push small and often.
5. When the TODO is done, write `## DONE` at the end of REPORT.md, push, and tell the human:
   "L<n> done - tell the Lead to read lab/<Ln>".

## Hard rules

- **Never edit `src/`, `tests/`, `packaging/`, `.github/`, `docs/`** or anything outside `lab/coord/<Ln>/`
  and `lab/reports/`. Found a bug? Report it (BUG section); the Lead fixes it.
- **Never push to `main`.** Only to `lab/<Ln>`.
- **Never type passwords, tokens or keys**, never change security settings, firewall rules, Secure Boot,
  drivers or accounts yourself. Say exactly what the human must click or type, and wait.
- **Never fake or "fix up" a result.** A failed step is reported as FAIL with the real output. Do not
  rerun until it passes and report only the pass: report every attempt.
- Do not delete files, uninstall things or reboot unless the TODO says so and the human agrees.
- Physical actions (close the lid, turn Wi-Fi off, unplug power, reboot, move the laptop) are done by the
  human. Ask, then wait for "done".
- Network: **Wi-Fi only** (D425). Record band (2.4/5 GHz), signal strength if shown, and whether the
  laptops are in the same room.
- Keep secrets out of reports: no tokens, pairing codes, private keys, passwords, full home paths of other
  people. Pairing codes may be used but never written down.
- If a TODO step is unclear or impossible, write it under QUESTIONS, push, and stop that step.

## REPORT.md format (append, never rewrite earlier entries)

```
### <step id> - <short name> - OK | FAIL | SKIPPED | BLOCKED
when: 2026-11-01 14:05 (local)
laptop: L2 (Windows 11, hostname ...)
commands:
    <exact commands run>
result: <one or two lines: what happened, key numbers>
evidence: <paths of saved files>
BUG: <only if something misbehaved: what, exact error text, how to reproduce>
QUESTIONS: <only if needed>
```

## Laptops

| id | role | OS |
|---|---|---|
| L1 | controller + agent, Lead session lives here | Linux |
| L2 | agent | see `lab/coord/L2/TODO.md` |
| L3 | agent | see `lab/coord/L3/TODO.md` |
| L4 | agent (arrives later) | see `lab/coord/L4/TODO.md` |

Fill in your OS/hostname/IP in the first step of your TODO; the Lead uses them for the next TODOs.
