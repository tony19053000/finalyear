"""What ``compute run --follow`` exits with (V0.4 G5, D76).

Kept as an import path for existing callers; the function and the whole exit
code table live in :mod:`meshforge.cli.exits` since V0.8 G6 (one mapping
module, D178).
"""

from __future__ import annotations

from meshforge.cli.exits import MAX_WORKLOAD_EXIT, job_exit_code

__all__ = ["MAX_WORKLOAD_EXIT", "job_exit_code"]
