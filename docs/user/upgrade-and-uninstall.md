# Upgrade or uninstall MeshForge

## Which versions work together

A controller accepts agents of **its own version line and the one before** (controller 0.17.x accepts agents
0.17.x and 0.16.x). An older agent, or an agent **newer** than the controller, is refused with
`version_unsupported` and stops (it does not retry over and over); its console and `meshforge-agent doctor` say
which side to upgrade. The dashboard's Nodes page shows each agent's version, with **update available** when it
is one version line behind.

So: **upgrade the controller first, then the laptops.**

## Upgrade

Install the new version over the old one - the same way you installed it. Settings, the laptop's identity (it
stays the same node), the owner settings and the controller's database are kept.

- **Ubuntu:** `sudo apt install ./meshforge-agent_<new>_amd64.deb` (or `meshforge-controller_...`).
- **Windows:** run the new `MeshForge-Agent-Setup-<new>.exe`.

What happens:

1. The version that runs now is kept aside (Linux: `/opt/meshforge/.previous/`; Windows: the old
   `C:\Program Files\MeshForge\<version>\` folder).
2. The controller backs up its database (`/var/lib/meshforge/db/backups/pre-<new>.dump`, the newest 3 kept).
3. The new version starts and must be **healthy** within about a minute: running, its local page answering
   with the new version, and (a joined laptop) its fabric part started; the controller must serve after its
   database migrations.
4. Healthy: the old copy is removed. The install says `... is running version <new>`.

### If the new version does not start: automatic rollback

MeshForge switches back to the version that ran before, by itself, and says so:

```text
meshforge-agent: UPGRADE FAILED: version 0.17.1 did not become healthy: the local console did not answer on 127.0.0.1 within 60 s.
meshforge-agent: ROLLED BACK: the previous version 0.17.0 runs again (/opt/meshforge/current -> .previous/0.17.0);
meshforge-agent: the identity, settings and jobs are unchanged. `meshforge-agent doctor` shows this until
meshforge-agent: an upgrade succeeds. Send the log above to the MeshForge team.
```

On Windows the installer's last page says *"The new version did not start; the previous version runs again."*
For the controller, the database is first restored from the backup taken before the upgrade, then the previous
version starts on it; laptops reconnect by themselves.

The laptop keeps working. `meshforge-agent doctor` (and the laptop's MeshForge page) shows
*"running the previous version ..."* until an upgrade succeeds. Fix the cause the message names, then install the
new version again (Ubuntu: `sudo apt install --reinstall ./<new>.deb`).

### If the upgrade was cut off (power loss, a killed installer)

- **Ubuntu:** at the next start MeshForge runs the version kept aside, and `doctor` says the upgrade was
  interrupted and how to finish it. Cut off while the new files were being unpacked (the usual case):
  install the new package again, `sudo dpkg -i ./meshforge-agent_<new>_amd64.deb` (then
  `sudo apt --fix-broken install` if apt asks). Cut off after the new version was already switched on: `sudo dpkg
  --configure -a`.
- **Windows:** run the installer again. It finishes the upgrade, or goes back to the previous version if the new
  one is not healthy.

### Forcing a rollback (testing only)

To see the rollback work, an administrator can make the next upgrade fail on purpose: put the new version into
the file `fail-upgrade-to` in the program folder's upgrade folder, then install the new version.

- Ubuntu agent: `sudo install -d /opt/meshforge/.upgrade && echo <new version> | sudo tee /opt/meshforge/.upgrade/fail-upgrade-to`
- Ubuntu controller: the same under `/opt/meshforge/controller/.upgrade/`
- Windows (administrator PowerShell):
  `New-Item -ItemType Directory -Force "$env:ProgramFiles\MeshForge\upgrade"; Set-Content "$env:ProgramFiles\MeshForge\upgrade\fail-upgrade-to" "<new version>"`

The file is used once and removed; the next install of that version is a normal upgrade. It is never a
setting, and only an administrator can create it.

## Uninstall

By default an uninstall **keeps your data** (the laptop's identity, so it comes back as the same node; the
controller's fabric and database). Remove everything only if you mean it.

| | Keep the data (default) | Remove everything |
|---|---|---|
| Ubuntu agent | `sudo apt remove meshforge-agent` | `sudo apt purge meshforge-agent` |
| Ubuntu controller | `sudo apt remove meshforge-controller` | `sudo apt purge meshforge-controller` (deletes the fabric!) |
| Windows | Settings → Apps → MeshForge Agent → Uninstall → *also remove all data?* **No** | the same, answer **Yes** |

With both packages on one computer, remove them in any order; each removes only its own files (the shared
`meshforge` user goes with the last one).

### Check that nothing is left

From a MeshForge source checkout (or the `lab/verify_uninstall.py` file from the release page), on the computer
you uninstalled:

```bash
sudo python3 lab/verify_uninstall.py --os linux                  # after apt remove
sudo python3 lab/verify_uninstall.py --os linux --data-removed   # after apt purge
python lab\verify_uninstall.py --os windows                      # Windows, administrator prompt
```

It ends with `CLEAN: nothing left behind` or lists every leftover (a service, a process, a firewall rule, a
file). With two packages on one computer, `--package agent` or `--package controller` checks the one you
removed.
