"""Controller-managed PostgreSQL (D390) - **spike SP2 prototype, not wired in yet**.

The controller runs a bundled PostgreSQL 16 as its own child: ``initdb`` once,
loopback + unix socket only, generated password, clean stop, refusal of another
major's data dir, recovery from a crashed controller. Evidence and design notes:
``lab/spikes/SP2.md``. G2 (roadmap 0.17.3) wires it behind
``[controller.db] mode = "managed"``.

Modules: :mod:`.binaries` (pinned bundle, version check), :mod:`.layout` (paths),
:mod:`.secret` (password file), :mod:`.pidfile` (``postmaster.pid``/``PG_VERSION``),
:mod:`.commands` (argv/env, parent-death shim), :mod:`.procs` (orphan detection),
:mod:`.cluster` (lifecycle), :mod:`.errors`.
"""

from __future__ import annotations

from meshforge.controller.db_managed.binaries import (
    BUNDLED_MAJOR,
    LINUX_X86_64,
    WINDOWS_X86_64,
    PgBinaries,
    PgBuild,
)
from meshforge.controller.db_managed.cluster import (
    ManagedCluster,
    ReconcileKind,
    ReconcileOutcome,
    StartOutcome,
    StopOutcome,
    StopTimeouts,
)
from meshforge.controller.db_managed.errors import (
    ClusterVersionMismatchError,
    ManagedDbError,
    ManagedDbReason,
)
from meshforge.controller.db_managed.layout import ManagedLayout

__all__ = [
    "BUNDLED_MAJOR",
    "LINUX_X86_64",
    "WINDOWS_X86_64",
    "ClusterVersionMismatchError",
    "ManagedCluster",
    "ManagedDbError",
    "ManagedDbReason",
    "ManagedLayout",
    "PgBinaries",
    "PgBuild",
    "ReconcileKind",
    "ReconcileOutcome",
    "StartOutcome",
    "StopOutcome",
    "StopTimeouts",
]
