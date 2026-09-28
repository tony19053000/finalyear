# Parked tester files (V0.4, 2026-09-28)

The G2 and G3 tester round-1 runs were **stopped part-way** on the team's request (token use: run one group at a
time). These are their unfinished test files, **never run to completion or reviewed**. They are renamed `*.py.txt`
and kept out of `tests/` so pytest and ruff ignore them.

The next G2 / G3 tester round should read them, move the useful ones back to the path encoded in the file name
(`tests_unit_common_test_common_bundle_g2_tester.py.txt` → `tests/unit/common/test_common_bundle_g2_tester.py`),
make them pass or report the defect they found, and delete this folder.

At the stop: the G2 tester reported its unit gate list "all passing, exit 0" and had just started the component
gates; the G3 tester had finished writing tests and was about to run its targeted suites.
