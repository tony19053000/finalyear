"""Benchmark system (V0.9, ADR-0018): workload families and, from G3, campaigns.

Imports only :mod:`meshforge.common` (``[tool.importlinter]``). ``numpy`` and
``torch`` are imported only when a workload runs, so the package imports on a
plain install (``compute bench workloads`` lists families without either).
"""
