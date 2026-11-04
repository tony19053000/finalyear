#!/usr/bin/env bash
# V0.7 part X on L1 (L2 on hold): captures on L1 while enrolment, REST/blobs, control and peer traffic happen.
# Secrets/codes stay in this scratch folder only; nothing here goes to git except the counts.
set -u
X=$(dirname "$(readlink -f "$0")"); cd "$X"
C=/opt/meshforge/controller/current/bin/compute
L4=node_01M6GGJP3FW5VHBDFKYQ6SF318; L3=node_01M6K0TZRNPEGBRN166GP6S61N
IF=wlp0s20f3
sudo -v || exit 1
( while sleep 50; do sudo -n -v; done ) & KEEP=$!

# markers + project + secret
MK_ARGV=MF-LAB-ARGV-$RANDOM$RANDOM; MK_FILE=MF-LAB-FILE-$RANDOM$RANDOM
echo "$MK_ARGV" > mk_argv; echo "$MK_FILE" > mk_file
rm -rf proj sleeper out; mkdir -p proj sleeper out
echo "$MK_FILE" > proj/data.txt
cat > proj/main.py <<'PY'
import os, sys
arg = sys.argv[1]; data = open("data.txt").read().strip()
print("argv", arg); print("file", data); print("secret", os.environ.get("LAB_KEY", "<file>"))
open("result.txt", "w").write(arg + " " + data + "\n")
PY
printf 'name: v07-markers\nentrypoint: ["python", "main.py", "%s"]\noutputs: [result.txt]\nsecrets: [LAB_KEY]\n' "$MK_ARGV" > proj/compute.yaml
printf 'import time\nprint("sleeping", flush=True)\ntime.sleep(300)\n' > sleeper/main.py
printf 'name: v07-sleeper\nentrypoint: python main.py\n' > sleeper/compute.yaml
printf 'MF-LAB-SECRET-%s' "$RANDOM$RANDOM" > val
$C secret set LAB_KEY < val && echo "secret LAB_KEY set"

# captures
sudo tcpdump -i $IF -s0 -w l1.pcap 'tcp port 47100 or tcp port 47101 or tcp port 47110' 2>tcpdump-l1.err & T1=$!
sleep 2

# a. enrolment on a throwaway data dir (dev CLI, user mode)
sudo tcpdump -i any -s0 -w enroll.pcap 'tcp port 47101' 2>tcpdump-en.err & T2=$!
sleep 2
rm -rf enroll-data; mkdir enroll-data
printf 'data_dir = "%s/enroll-data"\n' "$X" > enroll.toml
$C fabric invite --ttl 15m --json > invite.json
python3 -c "import json;print(json.load(open('invite.json'))['code'])" > mk_code
python3 -c "import json;print(json.load(open('invite.json'))['invite_id'])" > invite_id
( cd ~/mf-lab-L1 && MESHFORGE_INSTALL_MODE=user uv run meshforge-agent join --config "$X/enroll.toml" "$(cat "$X/mk_code")" ) > enroll-join.txt 2>&1
echo "join exit $?"; tail -3 enroll-join.txt
( cd ~/mf-lab-L1 && MESHFORGE_INSTALL_MODE=user uv run meshforge-agent identity show --config "$X/enroll.toml" ) > enroll-identity.txt 2>&1
grep -E 'node_id|enrolled' enroll-identity.txt
sleep 2; sudo kill -INT $T2; sleep 1

# b. REST + blobs + control: job on L4, logs, get
J=$($C run proj --node $L4 -f 2>&1 | tee job-l4.txt | grep -o 'job_[A-Z0-9]*' | head -1); echo "L4 job $J"; tail -4 job-l4.txt
$C logs $J > logs-l4.txt 2>&1; $C get $J --dest out/ > get-l4.txt 2>&1; echo "get exit $?"
# c. job on L3 (peer path if L3 fetches from a peer), then sleeper on L3 + cancel
J3=$($C run proj --node $L3 -f 2>&1 | tee job-l3.txt | grep -o 'job_[A-Z0-9]*' | head -1); echo "L3 job $J3"; tail -4 job-l3.txt
$C logs $J3 > logs-l3.txt 2>&1
JS=$($C run sleeper --node $L3 2>/dev/null | grep -o 'job_[A-Z0-9]*' | head -1)
for i in $(seq 1 40); do s=$($C jobs $JS --json | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('job',d)['state'])"); [ "$s" = running ] && break; sleep 2; done
sleep 5; $C cancel $JS | tail -1
sleep 5; sudo kill -INT $T1; sleep 2
kill $KEEP 2>/dev/null
sudo chown "$USER" l1.pcap enroll.pcap
echo "$J $J3 $JS" > jobs
echo "CAPTURE DONE"
