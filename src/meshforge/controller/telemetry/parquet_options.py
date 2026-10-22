"""The pinned Parquet writer options of every MeshForge dataset file (D222).

A module of constants only (no database, no pyarrow import), so pure modules
that encode Parquet - the V0.15 ML dataset (:mod:`meshforge.controller.ml`) -
share the exact options of the telemetry and labels exports without importing
their database code. :mod:`.export` re-exports both names.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["ROW_GROUP_SIZE", "WRITER_OPTIONS"]

WRITER_OPTIONS: Final[dict[str, Any]] = {
    "version": "2.6",
    "compression": "zstd",
    "compression_level": 3,
    "use_dictionary": False,
    "write_statistics": True,
    "data_page_version": "1.0",
    "store_schema": True,
}
"""Pinned Parquet writer options (D222): changing one changes the bytes."""
ROW_GROUP_SIZE: Final = 65_536
"""Rows per Parquet row group (pinned, D222): the file's layout, and the most
rows :func:`~.export.write_records` holds as Arrow data at once."""
