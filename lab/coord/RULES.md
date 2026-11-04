# Lab agent rules (D426)

You are the **lab agent** for ONE laptop (L1, L2, L3 or L4) in the MeshForge lab. The Lead session (on L1's
main repo) plans the work; you run it on your laptop and report. A human from the team sits next to you.

## Your loop - keep going, do not stop after every step

You work through the WHOLE TODO on your own, top to bottom, version after version. Do not ask the human
"what next?" and do not wait for the Lead between steps. Push your report quietly as you go.

1. Start: `git fetch origin`, `git checkout lab/<Ln>` (first time: `git checkout -b lab/<Ln> origin/main`),
   `git merge origin/main`. Read `lab/coord/<Ln>/TODO.md`.
2. Do the steps in order. After each step append the result to `lab/coord/<Ln>/REPORT.md` and
   `git add lab/ && git commit -m "lab(<Ln>): <step id> <ok|FAIL>" && git push origin lab/<Ln>`. Do NOT stop or
   ask anything after a successful step; go straight to the next one.
3. Every ~30 minutes, and at every `LEAD CHECKPOINT`, run `git fetch origin && git merge origin/main` so you
   pick up Lead fixes/new TODO lines without being told. After every merge, read `lab/coord/ANNOUNCE.md`
   (Lead messages to all laptops, newest on top). Do not stop at a checkpoint: write the SIGNAL line,
   push, and continue.
4. `WAIT:` steps: check the condition yourself (`git fetch origin`, read the other laptop's REPORT on
   `origin/lab/<Lx>`) every 2-3 minutes. While waiting, you may do later steps that do not depend on it.
   Only tell the human if you have waited more than 30 minutes.

**Stop and talk to the human ONLY when:**
- a `HUMAN:` step needs a physical action, a click, an admin/UAC/sudo prompt or a typed code;
- a step FAILS in a way that blocks the steps after it (report it in REPORT.md with BUG, push, then tell the
  human: "Blocked at <step> - tell the Lead to read lab/<Ln>". A failure that does NOT block later steps:
  report it, push, and keep going);
- a WAIT has lasted more than 30 minutes;
- the TODO is finished (`## DONE`, push, "L<n> finished - tell the Lead").

Keep messages to the human to one or two short lines.


## Auto-watch (2026-11-05) - how you notice new work without the human

Your session runs a 2-minute loop (`/loop 2m ...`, started by the human once). On every tick:
1. `git fetch origin && git merge origin/main` (on your branch).
2. Read the top of `lab/coord/ANNOUNCE.md` and the newest entries of `origin/lab/L1` REPORT.md. Look for
   `ACTION <Ln>` items or `SIGNAL: ...-go <Ln>` lines addressed to you that you have not recorded yet.
3. If there is one: do it now. If it needs the human (admin/UAC, physical action, a code), show the exact command
   in one short message and wait; then record the result in your REPORT, emit the requested SIGNAL, push.
4. If there is nothing new: continue your TODO; if you are waiting, say nothing (no message to the human).
Time-critical cues from L1 (`...-go <Ln>`) always come first.

**pause / start / stop (human words):**
- `pause` -> cancel your auto-watch loop (CronDelete / stop the /loop), finish or safely stop the current step,
  push, write `PAUSED <time>` in REPORT, then stay idle and silent. No checks, no tokens.
- `start` -> `git fetch && git merge origin/main`, re-read ANNOUNCE top, then re-create the auto-watch loop yourself
  (every 2 min, or the interval the human says, e.g. `start 5m`) and continue. Write `STARTED <time>`.
- `stop` -> same as pause, and also tell the human in one line where you stopped (step id).


## Lab chat (2026-11-05) - human words `send` and `read`

Messages between laptops (and the Lead) live on the shared branch **`lab/chat`**, folder `lab/chat/`, **one file
per message** (so nobody conflicts). You may push to `lab/chat` (the only exception to "only your own branch").

**`send <to>`** (to = L1, L2, L3, L4, LEAD or ALL; plain `send` = to whoever your current step needs):
1. `git fetch origin lab/chat` and work in a separate worktree so your own branch is untouched:
   `git worktree add -f ../mf-chat origin/lab/chat` (first time) or `git -C ../mf-chat pull --rebase origin lab/chat`.
2. Write `lab/chat/<YYYYMMDD-HHMMSS>-<FROM>-to-<TO>.md` with:
   ```
   # <FROM> -> <TO>: <one-line subject>
   time: <local time>   step: <your step id>   reply-needed: yes|no
   ## What I need
   <plain description>
   ## Do this (exact commands; mark HUMAN/Administrator lines)
   ## Expected result
   ## Reply with
   <what the other side must send back, e.g. the outputs + SIGNAL line>
   ```
3. `git add`, commit `chat: <FROM> -> <TO> <subject>`, push to `lab/chat` (on rejection: `pull --rebase`, push
   again). Tell the human in one line: "Sent to <TO>. Tell <TO> to `read`."

**`read`**: fetch `lab/chat`, list files addressed to you or ALL that are not in your `lab/coord/<Ln>/chat-read.txt`
(on your own branch); show each in 2-3 lines; then do the tasks in order (HUMAN steps: show exact commands, wait),
record results in your REPORT as usual, add the file names to `chat-read.txt`, push your branch. If the sender asked
for a reply, tell the human: "Done. Type `send <FROM>` to reply."

Never put secrets in chat (passwords, tokens, valid invite codes). Deliberately invalid test codes are fine.

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
