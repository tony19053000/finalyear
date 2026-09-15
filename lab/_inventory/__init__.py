"""Helpers for ``lab/inventory.py`` (roadmap task 0.2.2).

Layout (small modules, pure parsers separated from I/O so they are unit-testable
on any OS with recorded outputs):

* :mod:`_inventory.model`      probe result shape, schema version, LAB.md field map
* :mod:`_inventory.runner`     subprocess runner (timeouts, no shell, never raises)
* :mod:`_inventory.sanitize`   privacy: home paths, public IPs, secret scrubbing
* :mod:`_inventory.parse_common`   nvidia-smi, docker, git, version strings
* :mod:`_inventory.parse_linux`    lscpu, /proc, /sys, ip -j, lspci, mokutil
* :mod:`_inventory.parse_windows`  Get-CimInstance / Get-NetAdapter JSON, wsl.exe
* :mod:`_inventory.probes`     the collectors (one isolated probe per section)
* :mod:`_inventory.summary`    short human summary for stderr

This package is lab tooling, not part of the ``meshforge`` distribution. It may
import ``meshforge.common`` (optional), never ``controller``/``agent``.
"""
