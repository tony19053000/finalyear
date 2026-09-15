# <Topic> — <YYYY-MM-DD>

> File: `lab/reports/YYYY-MM-DD-<topic>.md` · Roadmap task: <0.2.x> · Spike: <S1–S7 or —> · Author: <agent/person> · Git commit: `<short sha>`

## Goal
The question this run answers, and the ARCHITECTURE §27 question or ADR it informs.

## Machines & conditions
| Host | OS / build | Hardware relevant to this test | Link (Wi-Fi/Ethernet, speed) | Power / perf mode |
|---|---|---|---|---|
| | | | | |

Background load, time of day, and anything unusual:

## Commands run
Exact commands, per host and per OS (bash / PowerShell), in order:
```bash
# host: <hostname>
<command>
```

## Raw output location
`lab/reports/raw/YYYY-MM-DD-<topic>/` (one file per host; unedited)

## Results
Repetitions per data point: **n = <≥5>** — any row with n < 5 must be marked INVALID with the reason under "Notes on outliers or failed runs".

| Case / pair / payload | Metric (unit) | n | mean | stdev | min | max |
|---|---|---|---|---|---|---|
| | | | | | | |

Notes on outliers or failed runs (list them, don't hide them):

## Expected vs actual

<Expected output stated in the 🧑 handoff note, and whether the actual result matched it.>

## Conclusion
What the data shows, in 2–5 plain sentences. Include the answer to the Goal question and how confident we are.

## Decisions triggered
- [ ] ADR created/updated: <ADR-NNNN + one-line change> (Proposed until the team accepts it)
- [ ] ARCHITECTURE §27 question answered: <Q#>
- [ ] Roadmap *(calibrate)* thresholds / design change log updated: <what>
- [ ] `docs/LAB.md` tables updated
- Follow-up runs needed:
