#!/bin/bash
# Real test: mc (CPU), img (CPU), gpu (GPU) - L1 alone, L4 alone, L1+L4 split. Results -> results.txt
L1=node_01M6BMFVHJVZXC7AET3CFRE3AV; L2=node_01M6GCKN7PYT2J297BT241P436; L4=node_01M6GGJP3FW5VHBDFKYQ6SF318; L3=node_01M79X8WTANK64QH74W9YMHBG5
T=$HOME/mf-eval/tools
for job in mc img gpu; do
  d=$HOME/mf-eval/realtest/$job; cp $d/compute.yaml $d/compute.base 2>/dev/null; [ -f $d/compute.orig ] || cp $d/compute.yaml $d/compute.orig
  for setn in L1 L4 L1+L4; do
    cp $d/compute.orig $d/compute.yaml
    case $setn in
      L1) args="--node $L1";;
      L4) args="--node $L4";;
      L1+L4) args="--exclude-node $L2 --exclude-node $L3"; python3 - "$d/compute.yaml" <<'PY'
import sys,re
p=sys.argv[1]; s=open(p).read()
s=re.sub(r"(\n  combine: [^\n]+)", r"\1\n  min_nodes: 2\n  max_nodes: 2", s, count=1)
open(p,'w').write(s)
PY
      ;;
    esac
    j=$(compute run $args $d 2>&1 | grep -oE "job_[0-9A-Z]{26}"); t0=$(date +%s); echo "== $job $setn $j start $(date +%H:%M:%S)"
    sleep 4; compute explain $j --json 2>/dev/null | python3 $T/pred.py 2>/dev/null | head -1
    while compute jobs $j 2>&1 | grep -qE "state +\| (running|queued|placed|pending|preparing|collecting)"; do sleep 10; done
    echo "real_s $(( $(date +%s)-t0 )) $(compute jobs $j 2>&1 | grep -E '^\|state' | tr -s ' ')"
    journalctl -u meshforge-controller --since "-120min" --no-pager -o cat | python3 $T/chunks.py $j
  done
done
