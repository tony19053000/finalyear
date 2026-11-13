#!/bin/bash
# GPU quick test: gpu-big job at 480 / 960 items; L1 alone, L1+L2+L4, all 4. Records to results.txt
L1=node_01M6BMFVHJVZXC7AET3CFRE3AV; L2=node_01M6GCKN7PYT2J297BT241P436; L4=node_01M6GGJP3FW5VHBDFKYQ6SF318; L3=node_01M79X8WTANK64QH74W9YMHBG5
P=$HOME/mf-eval/tools
cd $HOME/mf-eval/gputest
mk(){ # items set
  local k=$3
  sed -e "s/name: gpu-big/name: gputest-$1-$2/" -e "s/stop: [0-9]*/stop: $1/" -e "s/chunk: {items: 4}/chunk: {items: 16}/" -e "s/item_seconds: 6/item_seconds: 2.5/" -e "s/timeout: 40m/timeout: 90m/" ../gpu-big/compute.yaml > compute.yaml
  [ -n "$k" ] && sed -i "s/^  combine: merge-json/  combine: merge-json\n  min_nodes: $k\n  max_nodes: $k/" compute.yaml
}
for combo in ${COMBOS:-"480 l1l2l4" "480 all4" "960 l1l2l4" "960 all4"}; do
  set -- $combo; size=$1; setn=$2; {
    case $setn in alone) mk $size $setn ""; args="--node $L1";; l1l2l4) mk $size $setn 3; args="--exclude-node $L3";; all4) mk $size $setn 4; args="";; esac
    j=$(compute run $args . 2>&1 | grep -oE "job_[0-9A-Z]{26}"); t0=$(date +%s); echo "== $size $setn $j $(date +%H:%M:%S)"
    sleep 4; compute explain $j --json 2>/dev/null | python3 $P/pred.py 2>/dev/null | head -1
    while compute jobs $j 2>&1 | grep -qE "state +\| (running|queued|placed|pending|preparing|collecting)"; do sleep 10; done
    echo "real_s $(( $(date +%s)-t0 )) $(compute jobs $j 2>&1 | grep -E '^\|state' | tr -s ' ')"
    journalctl -u meshforge-controller --since "-120min" --no-pager -o cat | python3 $P/chunks.py $j
    compute get $j --dest outputs/$j --force >/dev/null 2>&1; [ -f outputs/$j/results.json ] && grep -o '"gpu":"[^"]*' outputs/$j/results.json | sort | uniq -c
  }
done
