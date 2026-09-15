# lab/

Physical-lab tooling and evidence for the MeshForge fabric. The human summary lives in [`docs/LAB.md`](../docs/LAB.md).

```
lab/
  inventory.py      hardware/software inventory → JSON   (task 0.2.2)
  _inventory/       helpers for inventory.py (parsers, probes, privacy filter)
  spikes/           feasibility spikes S1–S7              (tasks 0.2.5–0.2.11)
  reports/          one markdown report per lab run
    TEMPLATE.md     copy this for every report
    raw/            unedited command output, per run and per host
    inventory/      <hostname>.json from inventory.py
```

## inventory.py

Run it from the repo root after a fresh clone and a plain `uv sync` (no extras needed; no admin rights needed).

```bash
# Linux / WSL (bash)
uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"
```
```powershell
# Windows (PowerShell), native, not inside WSL
uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"
```

| Flag | Meaning |
|---|---|
| `--out PATH` | write the JSON to `PATH` (parent folders are created). Without it, the JSON goes to stdout. |
| `--pretty` | indent the JSON (keys are always sorted, so output is deterministic) |
| `--no-summary` | do not print the short human summary to stderr |
| `--timeout SECONDS` | wall-clock budget for the whole run, 5–3600, default `60`. Each command gets its own cap or the time left, whichever is smaller. When the budget runs out, remaining commands are not started and show up as `skipped: run deadline exceeded` errors. Use a larger value on a very slow machine, for example when the first `import torch` takes minutes. |

**Exit codes:** `0` = inventory written, **even if some probes failed** (a failure is data, e.g. `nvidia-smi` not working); `1` = the output file could not be written; `2` = bad command-line usage.

**What it collects** (one section each): `git` (short commit, dirty flag), `os`, `wsl`, `cpu`, `memory`, `disks`, `gpus` (every GPU incl. integrated/non-NVIDIA), `nvidia` (nvidia-smi / NVML status and error text), `secure_boot`, `python`, `uv`, `torch` (version, CUDA build, `cuda.is_available()`), `docker` (client/server, reachable, runtimes incl. `nvidia`; no containers are started), `network` (adapter kind Wi-Fi/Ethernet, MAC, private IPv4, link speed, up/down), `power` (AC/battery), `tools` (git, node, npm, gh, iperf3, protoc, psql).

Every section looks like `{"ok": true, "source": "lscpu", "value": {...}}` or `{"ok": false, "source": "...", "error": "..."}`. `failed_sections` lists the failed ones. **`problems`** is a sorted list of short sentences about anything a human must fix, even when the probe itself worked. It covers an NVIDIA GPU whose driver is not working, a CUDA build of torch where `cuda.is_available()` is false, Docker installed but its daemon unreachable, and every failed section. The stderr summary prints it. Check `problems`, not only `failed_sections`: the `nvidia` section is `ok: true` with `driver_working: false` on a machine whose driver is blocked. `LAB_FIELDS` in `_inventory/model.py` maps each `docs/LAB.md` §1 row to the JSON fields that answer it.

**Sources:** standard library first. psutil and nvidia-ml-py are used when installed (`--extra agent`). torch is used when installed (`--extra gpu`). Otherwise the script falls back to OS tools. On Linux these are `/proc`, `/sys`, `lscpu`, `ip -j`, `lspci -mm` and `mokutil`. On Windows it is one PowerShell `Get-CimInstance` / `Get-NetAdapter` call. `nvidia-smi` is used on both. Each command has its own timeout (15 s by default) and the whole run has a budget (`--timeout`). A command runs in its own process group, and a timed-out command is killed together with its child processes, so nothing is left running. A tool that timed out once is skipped for the rest of the run. No shell is used. torch and NVML run in a child process, so a broken GPU stack cannot hang or crash the run.

**Privacy (docs/LAB.md §6):** no Wi-Fi SSIDs or keys, usernames, serial numbers, environment variables or public IPs are collected. The home directory is written as `~` and other user folders as `<user>`. Public IPv4 addresses become `<public-ip>`. Secret-looking text is scrubbed with `meshforge.common.redaction`. MAC addresses, private IPv4 addresses and the hostname are kept on purpose. Known gap: a 4-part version string that looks like a public IP (for example `1.2.3.4`) is masked too.

**Windows notes:** Secure Boot is read from the registry, which is readable without admin. If the registry can't be read, `Confirm-SecureBootUEFI` is tried, and it usually reports `unknown` without admin. `Win32_VideoController.AdapterRAM` caps at 4 GiB, so for NVIDIA GPUs the nvidia-smi row replaces it. Integrated GPUs keep the CIM value, with a `vram_note` when it is capped. CPU `max_mhz` is `null` on Windows because turbo clocks are not exposed; `base_mhz` is the rated clock.

## Conventions
- **Report name:** `reports/YYYY-MM-DD-<topic>.md`, e.g. `2026-09-20-S1-network.md`.
- **Raw output:** `reports/raw/YYYY-MM-DD-<topic>/<hostname>.txt|json`. Never edit raw output by hand.
- **Repetitions:** at least 5 per measurement, reported as mean ± stdev.
- **Reproducibility:** every report records the git commit, the exact commands, the machines, and the link type (Wi-Fi/Ethernet).
- Lab scripts are not part of the `meshforge` package. They must not import `controller` or `agent`.
- No secrets in any file here (see docs/LAB.md §6).

## Running a 🧑 lab step
Follow [docs/LAB.md §5](../docs/LAB.md#5-lab-procedures--human-steps): run the exact commands from the task handoff, save the raw output, paste it back, and the agent writes the report from `reports/TEMPLATE.md`.
