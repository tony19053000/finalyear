"""Shared spike harness (roadmap V0.2 testing rule).

* :mod:`.stats` - summary statistics (n, mean, stdev, min, max, median, p95).
* :mod:`.reps` - warmup + repetition runner with typed, recorded failures.
* :mod:`.envelope` - the result JSON every spike writes (schema, git, host, link).
* :mod:`.report` - fills ``lab/reports/TEMPLATE.md`` from result JSONs.

Every spike: ``>= MIN_VALID_REPS`` measured repetitions per data point, warmup
excluded, raw samples kept, results under ``lab/reports/raw/<day>-<topic>/``.
"""

from typing import Final

#: A data point with fewer measured repetitions is reported as INVALID.
MIN_VALID_REPS: Final = 5
