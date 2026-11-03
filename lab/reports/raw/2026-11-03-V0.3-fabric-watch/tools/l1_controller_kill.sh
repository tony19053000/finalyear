#!/usr/bin/env bash
# C3.15-b on L1: at each ">>> KILL" prompt of restart-check, hard-kill the controller (systemd restarts it after 5 s).
set -u
cd ~/mf-lab-L1
export MESHFORGE_CLI__CONTROLLER_URL=https://192.168.31.13:47110
LOG=$(mktemp); sudo -v || exit 1
( while sleep 60; do sudo -n -v; done ) & KEEP=$!
uv run python -u lab/fabric_check.py --name L1 restart-check --link wifi </dev/null >"$LOG" 2>&1 & W=$!
tail -f "$LOG" & T=$!
seen=0
while kill -0 $W 2>/dev/null; do
  n=$(grep -c 'KILL the controller now' "$LOG")
  if [ "$n" -gt "$seen" ]; then seen=$n; sleep 2
    sudo systemctl kill -s KILL meshforge-controller; echo "[script] controller killed (rep $n)"; fi
  sleep 0.5
done
wait $W; RC=$?
kill $T $KEEP 2>/dev/null
systemctl is-active meshforge-controller
echo "EXIT=$RC  (log: $LOG)"
