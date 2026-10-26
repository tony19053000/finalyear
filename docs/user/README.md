# MeshForge user guide

MeshForge turns the laptops you already have into one shared computer. One laptop runs the **controller**
(the fabric's coordinator, with a web dashboard); every laptop that shares its CPU or GPU runs the **agent**.
You send a program to the fabric, and MeshForge decides where it runs best: on your own laptop, on another
laptop, split across several laptops, or using another laptop's GPU.

Nothing runs on a laptop unless its owner allows it. Every laptop has its own settings page for that.

## Which page do I need?

| I want to... | Read |
|---|---|
| set up the controller (the first laptop of a new fabric) | [install-linux.md](install-linux.md), then [create-a-fabric.md](create-a-fabric.md) |
| add a Windows laptop | [install-windows.md](install-windows.md), then [join-a-laptop.md](join-a-laptop.md) |
| add a Linux laptop | [install-linux.md](install-linux.md), then [join-a-laptop.md](join-a-laptop.md) |
| decide what my laptop shares | [owner-settings.md](owner-settings.md) |
| run a program on the fabric | [run-a-workload.md](run-a-workload.md) |
| start jobs from my own Python program | [sdk-quickstart.md](sdk-quickstart.md) |
| install a newer version, or remove MeshForge | [upgrade-and-uninstall.md](upgrade-and-uninstall.md) |
| fix something that went wrong | [troubleshooting.md](troubleshooting.md) |

## What you need

- **Controller:** a laptop or PC with **Ubuntu 24.04** (64-bit). It should stay on while the fabric is used.
- **Agents:** Windows 10 (22H2) or 11 (64-bit), or Ubuntu 22.04 / 24.04 (64-bit). Docker and an NVIDIA GPU
  are optional: without them a laptop still shares its processor.
- All laptops on the **same network** (home or team Wi-Fi, or Ethernet). MeshForge does not work across the
  internet.

## Words used in these pages

- **Fabric** - your group of laptops: one controller and its agents.
- **Node** - one laptop in the fabric, as the dashboard shows it.
- **Join code** - a one-time code (`mf1-...`) that lets one laptop join. The fabric admin makes it.
- **Job** - one program you send to the fabric, with its files.
- **Console** - the small page each laptop serves to itself (from the MeshForge menu entry) for joining and
  for the owner settings. Only that laptop can open it.
- **Dashboard** - the controller's web page: nodes, jobs, invites, users.

For developers and operators there is a longer runbook: [../OPERATIONS.md](../OPERATIONS.md).
