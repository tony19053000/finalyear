# Network shaping for benchmark conditions (`netem:...`, V0.9 G4)

The `netem:delay=…,loss=…,rate=…` benchmark condition shapes a Linux laptop's egress toward the controller with
`tc qdisc … netem`. That needs root, so it is **off by default** and allowed only through **one narrow sudoers
rule** for the agent's user. The agent itself never runs as root. Windows nodes report `netem` as unsupported.

## What the agent runs (and nothing else)

With the default `tc_command = ["sudo", "-n", "/usr/sbin/tc"]`, exactly these three forms, where `<iface>` is
`[agent.conditions] netem_interface` and the netem options are built by the agent from validated numbers
(never from text a user typed):

```text
sudo -n /usr/sbin/tc qdisc show    dev <iface>
sudo -n /usr/sbin/tc qdisc replace dev <iface> root netem [delay <X>ms] [loss <Y>%] [rate <Z>mbit]
sudo -n /usr/sbin/tc qdisc del     dev <iface> root
```

`-n` = never prompt: without the rule `sudo` fails at once and the agent simply does not advertise `netem`.
Loopback is refused; before shaping, the agent checks that its traffic to the controller really leaves through
`<iface>` (otherwise the run is `condition_failed` and nothing is shaped). It reverts the qdisc when the attempt
ends, and at the next start if it crashed (`<data dir>/conditions/active.json`).

## Install (once per Linux laptop that may shape)

1. Find the interface toward the controller: `ip route get <controller-ip>` → the word after `dev`
   (e.g. `wlp2s0` on Wi-Fi, `enp0s31f6` on Ethernet). Check the tc path: `command -v tc` (Ubuntu:
   `/usr/sbin/tc`).
2. Create the rule with `sudo visudo -f /etc/sudoers.d/meshforge-netem` (replace `meshforge` with the user the
   agent runs as, and `wlp2s0` with your interface - in all three lines):

   ```sudoers
   # MeshForge benchmark network shaping (V0.9 G4): three tc forms on ONE interface.
   Cmnd_Alias MESHFORGE_TC = /usr/sbin/tc qdisc show dev wlp2s0, \
                             /usr/sbin/tc qdisc del dev wlp2s0 root, \
                             /usr/sbin/tc qdisc replace dev wlp2s0 root netem *
   meshforge ALL=(root) NOPASSWD: MESHFORGE_TC
   ```

   `visudo` refuses a file with a syntax error. The `*` covers only the netem options after `netem` (tc parses
   them as netem options; its global options must come before `qdisc`, so they cannot be smuggled in there).
3. In the agent's config:

   ```toml
   [agent.conditions]
   netem = true
   netem_interface = "wlp2s0"
   ```

4. Restart the agent. `compute nodes --json` shows `bench-conditions:…netem…` in its capabilities; if not, the
   agent log has `agent.conditions.netem_unavailable` with the reason.

To remove it: delete `/etc/sudoers.d/meshforge-netem` and set `netem = false`.

## Residual risk (accepted)

Anyone who can run commands as the agent's user can add delay/loss to that one interface (no other command,
no other interface) - a nuisance on the owner's own laptop, not a privilege escalation. See THREAT_MODEL
`RE-E4`.
