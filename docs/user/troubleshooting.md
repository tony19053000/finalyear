# Troubleshooting

Each entry starts with what you see. Where MeshForge shows a **code** (on the laptop's MeshForge page, in
`meshforge-agent doctor`, on the installer's last page, or in a command's output), it is listed as
`Code: ...` so you can search this page for it.

First step for almost anything: **`meshforge-agent doctor`** on the laptop (Ubuntu: any terminal; Windows:
an administrator PowerShell) and, on the controller, **`sudo meshforge-controller doctor`**. They change nothing
and say what is wrong.

> A test (`tests/unit/test_v017_g5_troubleshooting_codes.py`) checks that every code on this page exists in
> MeshForge and that every code MeshForge can show has an entry here.

## Installing

### "Windows protected your PC" (SmartScreen)
The installer is not signed yet. Click **More info**, check the name `MeshForge-Agent-Setup-...`, click **Run
anyway**.

<!-- TODO-SCREENSHOT: windows-smartscreen.png - the SmartScreen dialog after "More info" (🧑 0.17.8 lab) -->

### `apt` refuses the controller: `Depends: libc6 (>= 2.36)`
The controller needs Ubuntu 24.04. The agent package works on 22.04 too.

### The installer's last page: "installed but NOT running yet"
Code: `needs_attention`
MeshForge stopped on purpose - usually it found two older MeshForge identities on this computer (two users ran
it before) and will not guess which one is this laptop. The page names both folders and the command to pick one.

### The installer's last page: "installed but NOT running"
Code: `failed`
The service did not start and could not be brought back. The page says why (for example a service that would
not stop). Restart the computer and run the installer again; if it repeats, send
`C:\ProgramData\MeshForge\logs\setup-result.json` to the MeshForge team.

## Upgrading

### "UPGRADE FAILED ... ROLLED BACK" / "The new version did not start; the previous version runs again"
Code: `rolled_back`
The new version was not healthy, so MeshForge went back to the version that ran before. Nothing is lost: same
node, same settings, same jobs. The message says what was wrong. Fix that, then install the new version again
(Ubuntu: `sudo apt install --reinstall ./<file>.deb`). See [upgrade-and-uninstall.md](upgrade-and-uninstall.md).

### "an upgrade ... was interrupted"
Code: `interrupted`
The computer lost power (or the installer was stopped) in the middle of an upgrade. MeshForge runs the version it
kept aside, or the new one if it was already complete. Finish the upgrade the way the message says - Ubuntu:
install the new package again (`sudo apt install --reinstall ./<new>.deb` or `sudo apt --fix-broken install`)
when it was cut off while unpacking, `sudo dpkg --configure -a` when it was cut off after the switch; Windows:
run the installer again.

### Controller: "the database was restored from the backup taken before it"
Code: `upgrade_rolled_back`
The new controller version did not start after updating its database, so the database was put back as it was and
the previous version started again on it. Laptops reconnect by themselves.

### Controller: `db status` says ROLLBACK FAILED
Code: `rollback_failed`
After a failed upgrade the database could **not** be restored automatically. The controller stays stopped (it will
not start an older version on a database in an unknown state). `sudo meshforge-controller db status` names the
backup; restore it with `sudo meshforge-controller db restore <backup> --yes`, then
`sudo systemctl start meshforge-controller`.

## Joining a fabric (the laptop's MeshForge page)

### "This code has expired." / "This code was already used to join a laptop."
Codes: `expired`, `used`
A join code works once and only for a while (15 minutes by default). Ask the admin for a new one (dashboard:
**Settings > Invites > New invite**).

### "The fabric admin cancelled this code." / "The controller does not know this code."
Codes: `revoked_code`, `bad_code`
Ask for a new code. `bad_code` also means a code that was cut short when copying: codes start with `mf1-`.

### "This laptop was removed from the fabric by its admin."
Code: `node_revoked`
Leave the fabric on this laptop (**Leave** in the page's menu), then join again with a new code.

### "This laptop has already joined a fabric."
Code: `already_joined`
To join a different fabric, leave the current one first.

### "Cannot reach the controller ..."
Code: `unreachable`
The controller computer is off, on another network, or its address changed. Check it is on and on the same Wi-Fi;
pick it from the list of controllers the page found on the network. A firewall on the controller must allow TCP
47100, 47101 and 47110 (the controller package opens them itself when `ufw` is on).

### "Something answered ..., but it is not a MeshForge controller's join port."
Code: `not_a_controller`
The address is wrong. Pick the controller from the list, or ask for a new code.

### "The controller ... is not the fabric this code belongs to"
Code: `ca_mismatch`
You picked a different controller than the one that made the code. Nothing secret was sent. Pick the right one.

### "The controller is busy ..." / "cannot accept joins right now"
Code: `busy`
Wait a minute and try again.

### "Joined, but this laptop could not save its certificate."
Code: `storage`
Usually a full disk. Free some space, then join again with a new code.

### "The controller refused to let this laptop join."
Code: `refused`
Ask the fabric admin to look at the controller's log (`journalctl -u meshforge-controller`).

## MeshForge stopped on this laptop (the page's Home)

### "version_unsupported: agent ... is too old for controller ..."
Code: `version_unsupported`
The controller accepts agents of its own version line and the one before. **Too old:** install the newer
MeshForge on this laptop. **"newer than controller":** the fabric admin upgrades the controller first. Then press
**Restart** on the page. The dashboard's Nodes page marks agents that are one version behind with
**update available**.

### "this node was revoked" / removed from the fabric
Code: `revoked`
The admin removed this laptop. To come back: **Leave the fabric**, then join with a new code.

### "another meshforge-agent with node_id ... connected"
Code: `conflict`
Two agents use the same identity - typically an old developer copy (`meshforge-agent run` in a terminal, or the
old Windows scheduled task) next to the installed service. Stop the other one, then press **Restart**.

### A setting is named as wrong
Codes: `config`, `config_reload`
MeshForge refuses a configuration value it cannot use and names it (and the file). Fix it (or ask whoever installed
MeshForge), then press **Restart**.

### "leaving failed: ..."
Code: `identity`
MeshForge could not remove this laptop's identity. See the **Logs** page; run `meshforge-agent doctor`.

### "the fabric connection ended" or another error
Codes: `ended`, `error`
Press **Restart**. If it stops again, the **Logs** page (or `journalctl -u meshforge-agent`) has the reason.

## Docker, GPU and network

### "docker: no access" / jobs do not run in containers
Docker access is the same as administrator rights, so MeshForge never takes it by itself. Ubuntu: app menu →
MeshForge → **Give Docker access**. Windows: Start menu → **MeshForge - give Docker access**. Docker Desktop on
Windows only runs while someone is signed in.

### The GPU check says the driver does not answer
Install or repair the NVIDIA driver and restart the laptop. Sharing the GPU stays off until you turn it on in the
owner settings.

### Windows: "the network is Public"
Other laptops cannot fetch files from this one on a network Windows calls *Public*; jobs still run. On your own
network: **Settings → Network & internet → (your network) → Network profile type → Private**.

### "needs an elevated prompt" / "only administrators may change" (Windows)
`meshforge-agent` commands that change the service need an administrator PowerShell (right-click → Run as
administrator). For everyday things use the MeshForge page from the Start menu instead.

### "cannot listen on 127.0.0.1:47130 for the local console ... is another MeshForge agent running here?"
Another program (often a second MeshForge agent) uses the console's port. Stop the other agent.

## The controller's database

`sudo meshforge-controller doctor` and `journalctl -u meshforge-controller -n 80` show these. Codes:

| Code | What it means | What to do |
|---|---|---|
| `port_in_use` | another program uses the database port (47150) | stop it, or set `[controller.db] port` in `/etc/meshforge/controller.toml` |
| `binaries_missing`, `binaries_unusable` | the bundled PostgreSQL is missing or cannot run (wrong Ubuntu) | reinstall the package; the controller needs Ubuntu 24.04 |
| `version_mismatch` | the database was made by another PostgreSQL major version | restore a backup made by this version, or ask the MeshForge team |
| `data_dir_invalid`, `path_invalid` | `/var/lib/meshforge/db` is not a MeshForge database, or a path PostgreSQL cannot use | do not edit that folder by hand; restore from a backup |
| `locked` | another controller already uses this database | stop the other one (a developer `meshforge-controller` in a terminal) |
| `initdb_failed`, `start_failed`, `start_timeout` | the database could not be created or did not start (in time) | `journalctl -u meshforge-controller -n 80` names the log file; often a full disk |
| `stop_failed`, `orphan_stop_failed` | an old database process would not stop | restart the computer |
| `not_running` | a `db` command needs the database, which is not running | `sudo systemctl start meshforge-controller` |
| `in_use` | `db restore` while the controller runs | `sudo systemctl stop meshforge-controller` first |
| `dump_failed`, `restore_failed` | a backup or a restore did not work (nothing was changed) | check the free disk space, then try again |

Still stuck? Collect `meshforge-agent doctor` (and `sudo meshforge-controller doctor`), the last 80 log lines, and
the installer's output, and send them to the MeshForge team.
