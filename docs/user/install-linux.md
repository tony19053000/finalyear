# Install MeshForge on Ubuntu

There are two packages. Install the one(s) this computer needs:

| Package | What it is | Ubuntu |
|---|---|---|
| `meshforge-agent_<version>_amd64.deb` | shares this computer's CPU/GPU with a fabric | 22.04 or 24.04 |
| `meshforge-controller_<version>_amd64.deb` | runs the fabric itself (one per fabric) | 24.04 only |

One computer may have both: the controller laptop can share its own CPU too.

You do **not** need Python, Docker, PostgreSQL or a source checkout: each package brings everything it needs.
You need an administrator account (one that can use `sudo`). Download the files from the release page (or get
them from whoever runs the fabric).

Check a download (optional): `sha256sum -c SHA256SUMS --ignore-missing` in the download folder must say `OK`.

## The agent (every laptop that shares)

```bash
sudo apt install ./meshforge-agent_<version>_amd64.deb
```

`apt` needs the `./` (it means "this file", not a package from the internet). The last lines say what is true:

```text
meshforge-agent: service meshforge-agent.service is running version 0.17.1 (logs: journalctl -u meshforge-agent)
meshforge-agent: this laptop has not joined a fabric yet: open "MeshForge" from the app menu
```

MeshForge now runs in the background as the system service `meshforge-agent`: it starts with the computer and
keeps running when you log out. It runs **nobody's jobs** until you allow it.

Next: **open "MeshForge" from the app menu** (or run `meshforge-console-open`) and join - see
[join-a-laptop.md](join-a-laptop.md). The person who ran `sudo apt install` may open that page. To let another
user open it: `sudo usermod -aG meshforge-admin <user>` (they log out and in once).

**Docker** (optional): giving MeshForge Docker access equals giving it administrator rights, so it is off until
you choose it: app menu → MeshForge → **Give Docker access**. See [owner-settings.md](owner-settings.md).

**A laptop that ran MeshForge from a source checkout before:** the package moves that identity into the service,
so the laptop stays the same node in the dashboard, and disables the old per-user service. If it finds two
identities (two users ran it), it installs but does not start MeshForge and prints both folders and the command
to choose one.

## The controller (one per fabric)

Ubuntu **24.04** (22.04 is refused: `apt` says `Depends: libc6 (>= 2.36)`).

```bash
sudo apt install ./meshforge-controller_<version>_amd64.deb
```

It writes its settings to `/etc/meshforge/controller.toml` (listening on this computer's network address),
starts its own database and the controller, and ends with:

```text
meshforge-controller: Create your fabric: run this to get the one-time setup link, then open it
meshforge-controller: in a browser ON THIS COMPUTER:
meshforge-controller:
meshforge-controller:     sudo meshforge-controller setup url
```

Continue with [create-a-fabric.md](create-a-fabric.md).

On a computer with only the controller package, the `compute` command is
`/opt/meshforge/controller/current/bin/compute` (the agent package puts `compute` on the PATH).

## Is it running?

```bash
systemctl status meshforge-agent          # or meshforge-controller
meshforge-agent doctor                    # version, joined or not, last upgrade, Docker/GPU/disk checks
sudo meshforge-controller doctor          # the controller's own checks
journalctl -u meshforge-agent -n 50       # the agent's recent log
```

## Other Linux distributions

Only Ubuntu 22.04/24.04 `.deb` packages are built and tested. On another distribution you can unpack the
bundle archive (`meshforge-<version>-linux-x86_64.tar.xz`) and run it by hand as described in
[../OPERATIONS.md](../OPERATIONS.md); that path is not tested.

Upgrading and removing: [upgrade-and-uninstall.md](upgrade-and-uninstall.md).
