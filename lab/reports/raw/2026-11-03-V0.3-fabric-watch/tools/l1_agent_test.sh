#!/usr/bin/env bash
# C3.7-b on L1 itself: stop/start the local agent 6x and press "Enter" for fabric_check at the same moment.
set -u
cd ~/mf-lab-L1
export MESHFORGE_CLI__CONTROLLER_URL=https://192.168.31.13:47110
D=$(mktemp -d); FIFO=$D/in; LOG=$D/out.log; mkfifo "$FIFO"
sudo -v || exit 1
( while sleep 60; do sudo -n -v; done ) & KEEP=$!
uv run python -u lab/fabric_check.py --name L1 watch --node JARVISM3 --action agent --link wifi --reps 6 <"$FIFO" >"$LOG" 2>&1 & W=$!
exec 3>"$FIFO"
tail -f "$LOG" & T=$!
seen_stop=0; seen_start=0
while kill -0 $W 2>/dev/null; do
  n=$(grep -c '>>> STOP' "$LOG"); if [ "$n" -gt "$seen_stop" ]; then seen_stop=$n; sleep 2
    echo >&3; sudo systemctl stop meshforge-agent; echo "[script] Enter + agent stopped"; fi
  n=$(grep -c '>>> START' "$LOG"); if [ "$n" -gt "$seen_start" ]; then seen_start=$n; sleep 3
    echo >&3; sudo systemctl start meshforge-agent; echo "[script] Enter + agent started"; fi
  sleep 0.5
done
wait $W; RC=$?
kill $T $KEEP 2>/dev/null; exec 3>&-
sudo systemctl is-active meshforge-agent >/dev/null || sudo systemctl start meshforge-agent
echo "EXIT=$RC  (log: $LOG)"
