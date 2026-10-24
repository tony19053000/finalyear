"""Spike SP2 (roadmap 0.17.0, D390/D393): controller-managed PostgreSQL 16.

Fetches (or reuses) the pinned relocatable PostgreSQL build, trims it, and runs
every SP2 scenario against the **trimmed** bundle, printing one JSON document
(pasted into ``lab/spikes/SP2.md``)::

    uv run python lab/spikes/sp2_managed_pg.py            # all scenarios
    uv run python lab/spikes/sp2_managed_pg.py --keep DIR # keep the scratch dir

Linux x86_64 only (the Windows build is only pinned, D390). Needs the controller
extra (asyncpg, alembic) and network access on the first run. No Docker.

Exit codes: ``0`` all scenarios ran; ``1`` a scenario failed (traceback printed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parents[1]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from meshforge.controller.db_managed import LINUX_X86_64  # noqa: E402
from spikes._sp2 import scenarios  # noqa: E402
from spikes._sp2.bundle import ensure_trimmed  # noqa: E402


async def _run_all(work: Path) -> dict[str, Any]:
    started = time.monotonic()
    bundle = await asyncio.to_thread(ensure_trimmed, LINUX_X86_64)
    fetch_s = time.monotonic() - started
    bin_dir = bundle.bin_dir
    result: dict[str, Any] = {
        "host": {
            "platform": platform.platform(),
            "libc": " ".join(platform.libc_ver()),
            "python": platform.python_version(),
        },
        "bundle": {
            "url": LINUX_X86_64.url,
            "sha256": LINUX_X86_64.sha256,
            "archive_bytes": bundle.archive_bytes,
            "unpacked_bytes": bundle.full_bytes,
            "trimmed_bytes": bundle.trimmed_bytes,
            "prepare_ms": round(fetch_s * 1000, 1),
        },
    }
    result["lifecycle"] = await scenarios.lifecycle(bin_dir, work / "lifecycle")
    result["version_mismatch"] = await scenarios.version_mismatch(
        bin_dir, work / "mismatch"
    )
    result["stale_pidfile"] = await scenarios.stale_pidfile(bin_dir, work / "stale")
    result["crash_without_death_signal"] = await scenarios.crash(
        bin_dir, work / "crash-plain", die_with_parent=False
    )
    result["crash_with_death_signal"] = await scenarios.crash(
        bin_dir, work / "crash-pdeath", die_with_parent=True
    )
    result["port_in_use"] = await scenarios.port_in_use(bin_dir, work / "port")
    result["concurrent_start"] = await scenarios.concurrent_start(
        bin_dir, work / "concurrent"
    )
    result["postmaster_killed"] = await scenarios.postmaster_killed(
        bin_dir, work / "pmkill"
    )
    async with scenarios.managed(bin_dir, work / "store") as cluster:
        result["migrate_and_smoke"] = await scenarios.migrate_and_smoke(cluster)
        result["dump_restore"] = await scenarios.dump_restore(
            cluster, cluster.layout.backups_dir
        )
        result["dump_restore_500k_rows"] = await scenarios.dump_restore(
            cluster, cluster.layout.backups_dir, bulk_rows=500_000
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", type=Path, help="scratch dir to use and keep")
    args = parser.parse_args(argv)
    work = args.keep or Path(tempfile.mkdtemp(prefix="sp2-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(_run_all(work))
    finally:
        if args.keep is None:
            shutil.rmtree(work, ignore_errors=True)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
