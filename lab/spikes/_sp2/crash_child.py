"""Stand-in "controller" for the SP2 crash scenario (run as a separate process).

argv: ``<bin_dir> <root> <socket_dir> <port> <die_with_parent 0|1>``. Starts the
managed cluster, commits one row to ``crash_probe``, prints one JSON line
``{"postmaster_pid": ..., "child_pid": ...}`` and sleeps until killed. The caller
SIGKILLs this process and watches what happens to the postmaster.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def _main(argv: list[str]) -> None:
    import asyncpg

    from meshforge.controller.db_managed import (
        ManagedCluster,
        ManagedLayout,
        PgBinaries,
    )

    bin_dir, root, socket_dir, port, die = argv
    cluster = ManagedCluster(
        PgBinaries(Path(bin_dir)),
        ManagedLayout(Path(root), socket_dir_override=Path(socket_dir)),
        port=int(port),
        die_with_parent=die == "1",
    )
    outcome = await cluster.start()
    settings = cluster.database_settings()
    conn = await asyncpg.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        database=settings.name,
    )
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS crash_probe (id serial PRIMARY KEY, note text)"
    )
    await conn.execute("INSERT INTO crash_probe (note) VALUES ('before-crash')")
    # Keep the connection open: a live client like the controller's pool.
    ready = {"postmaster_pid": outcome.pid, "child_pid": os.getpid()}
    sys.stdout.write(json.dumps(ready) + "\n")
    sys.stdout.flush()
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
