#!/bin/bash
# 5 repeats x {tiny, heavy} GPU job (160 items, chunk 16) x {L1, L4, L1+L4} through MeshForge -> gpu5x.txt
L1=node_01M6BMFVHJVZXC7AET3CFRE3AV; L2=node_01M6GCKN7PYT2J297BT241P436; L4=node_01M6GGJP3FW5VHBDFKYQ6SF318; L3=node_01M79X8WTANK64QH74W9YMHBG5; T=$HOME/mf-eval/tools
run(){ local d=$1; shift; j=$(compute run "$@" $d 2>&1 | grep -oE "job_[0-9A-Z]{26}"); t0=$(date +%s); while compute jobs $j 2>&1 | grep -qE "state +\| (running|queued|placed|pending|preparing|collecting)"; do sleep 3; done
  st=$(compute jobs $j 2>&1 | grep -E '^\|state' | awk '{print $4}'); ch=$(journalctl -u meshforge-controller --since "-30min" --no-pager -o cat | python3 $T/chunks.py $j | tr '\n' ' ')
  echo "$REP $KIND $CFG $j $st real_s=$(( $(date +%s)-t0 )) | $ch"; }
for REP in 1 2 3 4 5; do for KIND in tiny heavy; do
  d=$HOME/mf-eval/${KIND}gpu; cp $d/compute.orig $d/compute.yaml
  CFG=L1 run $d --exclude-node $L2 --exclude-node $L3 --exclude-node $L4
  CFG=L4 run $d --exclude-node $L1 --exclude-node $L2 --exclude-node $L3
  sleep 45; sed -i "s/^  combine: merge-json/  combine: merge-json\n  min_nodes: 2\n  max_nodes: 2/" $d/compute.yaml
  CFG=L1+L4 run $d --exclude-node $L2 --exclude-node $L3; cp $d/compute.orig $d/compute.yaml
done; done
