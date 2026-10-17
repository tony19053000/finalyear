"""Telemetry: run records and the Parquet dataset (V0.9 G1, D211-D213, ADR-0018).

* :mod:`.schema` - the columns of telemetry schema v1 (the one declaration);
* :mod:`.record` - one record from persisted rows (pure);
* :mod:`.recorder` - the reconciler that writes a record for every terminal
  attempt (``run_record``, migration 0021);
* :mod:`.export` - records -> a byte-reproducible Parquet file (pyarrow).

Nothing is imported here, so importing the package never pulls in pyarrow.
"""
