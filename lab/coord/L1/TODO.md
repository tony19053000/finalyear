# L1 TODO - round 1: this laptop alone (V0.2, Wi-Fi only)

Read `lab/coord/RULES.md` first. Work on branch `lab/L1`. Write results to `lab/coord/L1/REPORT.md`.
These steps need no other laptop. Plugged in, performance power mode, close other apps before R1-5.
Windows: use PowerShell (native, not WSL). Linux: bash.

- **R1-1 Who am I.** Record in REPORT.md: OS + version, hostname, CPU, RAM, GPU (if any), Wi-Fi IP address
  (`ip -4 addr` / `ipconfig`), Wi-Fi band and signal if shown (`nmcli dev wifi` / `netsh wlan show interfaces`),
  whether it is in the same room as L1. Windows: is the Wi-Fi network profile **Private**? (Settings > Network >
  Wi-Fi > properties.) If it is Public, ask the human to switch it to Private and record that.
- **R1-2 Tools.** `git --version`, `uv --version`, `uv sync` in the repo root (report errors in full),
  `uv run python --version`. Docker: `docker version` (just report if missing, do not install).
- **R1-3 Inventory (0.2.2).** Linux: `uv run python lab/inventory.py --out "lab/reports/inventory/$(hostname).json"`
  Windows: `uv run python lab/inventory.py --out "lab/reports/inventory/$env:COMPUTERNAME.json"`
  Report the exit code and 3-4 headline lines (CPU, RAM, GPU, disk).
- **R1-4 GPU check (0.2.1 / 0.2.3), only if this laptop has an NVIDIA GPU.** `nvidia-smi` (paste the header table).
  Then `uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"` (if torch is not
  installed, report that; do not install anything). If Docker exists: `docker run --rm --gpus all
  nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` (ask the human first; it downloads ~100 MB). No NVIDIA GPU:
  write SKIPPED with the reason.
- **R1-5 Hardware scores (0.2.11).** `uv run python lab/spikes/s7_hardware.py run --quick --name L1` then
  `uv run python lab/spikes/s7_hardware.py run --name L1` (1-5 min; add `--skip-gpu` if no NVIDIA GPU).
  Report the exit code and the scores printed on stderr. If it prints `!! ON BATTERY` or `thermal throttling`,
  ask the human to fix it and run again (report both runs).
- **R1-6 Environments (0.2.10), only if Docker is installed.** `uv run python lab/spikes/s6_environments.py run --quick --name L1`
  then `uv run python lab/spikes/s6_environments.py run --name L1`. No Docker: run it anyway (venv cases still
  run) and report the exit code and which cases were skipped.
- **R1-7 Push** everything (`lab/reports/` + REPORT.md), write `## DONE`, push, tell the human.

Round 2 (pairs of laptops: network S1, cross-OS S2, transport S3) comes after the Lead has everybody's IP.
