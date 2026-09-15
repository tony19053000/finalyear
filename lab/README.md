# lab/

Physical-lab tooling and evidence for the MeshForge fabric. The human summary lives in [`docs/LAB.md`](../docs/LAB.md).

```
lab/
  inventory.py      hardware/software inventory → JSON   (task 0.2.2, not yet written)
  spikes/           feasibility spikes S1–S7              (tasks 0.2.5–0.2.11)
  reports/          one markdown report per lab run
    TEMPLATE.md     copy this for every report
    raw/            unedited command output, per run and per host
    inventory/      <hostname>.json from inventory.py
```

## Conventions
- **Report name:** `reports/YYYY-MM-DD-<topic>.md`, e.g. `2026-09-20-S1-network.md`.
- **Raw output:** `reports/raw/YYYY-MM-DD-<topic>/<hostname>.txt|json`. Never edit raw output by hand.
- **Repetitions:** at least 5 per measurement, reported as mean ± stdev.
- **Reproducibility:** every report records the git commit, the exact commands, the machines, and the link type (Wi-Fi/Ethernet).
- Lab scripts are not part of the `meshforge` package. They must not import `controller` or `agent`.
- No secrets in any file here (see docs/LAB.md §6).

## Running a 🧑 lab step
Follow [docs/LAB.md §5](../docs/LAB.md#5-lab-procedures--human-steps): run the exact commands from the task handoff, save the raw output, paste it back, and the agent writes the report from `reports/TEMPLATE.md`.
