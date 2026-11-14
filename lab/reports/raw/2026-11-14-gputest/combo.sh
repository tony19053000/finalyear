#!/bin/bash
# GPU combination test: {tiny, heavy} (160 items, chunk 16) x 2 runs x {L1, L2, L4, L1+L2, L1+L4, L2+L4, L1+L2+L4} -> combo.txt
L1=node_01M6BMFVHJVZXC7AET3CFRE3AV; L2=node_01M6GCKN7PYT2J297BT241P436; L4=node_01M6GGJP3FW5VHBDFKYQ6SF318; L3=node_01M79X8WTANK64QH74W9YMHBG5; T=$HOME/mf-eval/tools
declare -A SET=( [L1]="$L1" [L2]="$L2" [L4]="$L4" [L1+L2]="$L1 $L2" [L1+L4]="$L1 $L4" [L2+L4]="$L2 $L4" [L1+L2+L4]="$L1 $L2 $L4" )
for CFG in L1 L2 L4 L1+L2 L1+L4 L2+L4 L1+L2+L4; do for KIND in tiny heavy; do for REP in 1 2; do
  d=$HOME/mf-eval/${KIND}gpu; cp $d/compute.orig $d/compute.yaml; members=(${SET[$CFG]}); k=${#members[@]}
  args=""; for n in $L1 $L2 $L4 $L3; do [[ " ${SET[$CFG]} " == *" $n "* ]] || args="$args --exclude-node $n"; done
  if [ $k -ge 2 ]; then sleep 45; sed -i "s/^  combine: merge-json/  combine: merge-json\n  min_nodes: $k\n  max_nodes: $k/" $d/compute.yaml; fi
  j=$(compute run $args $d 2>&1 | grep -oE "job_[0-9A-Z]{26}"); t0=$(date +%s)
  while compute jobs $j 2>&1 | grep -qE "state +\| (running|queued|placed|pending|preparing|collecting)"; do sleep 3; done
  ch=$(journalctl -u meshforge-controller --since "-30min" --no-pager -o cat | python3 $T/chunks.py $j | tr '\n' ' ')
  echo "$CFG $KIND $REP $j real_s=$(( $(date +%s)-t0 )) | $ch"; cp $d/compute.orig $d/compute.yaml
done; done; done
