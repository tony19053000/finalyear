"""The V1.0 evaluation harness (roadmap 1.0.6, D415-D419; brief
``docs/briefs/V1.0-release.md`` §5 G2).

Three entry scripts next to this package share it:

* ``scripts/eval/run_campaign.py`` - the live policy matrix (family x size x
  arm x repetition) on a real fabric, block-randomised, resumable; writes the
  raw data (:mod:`.raw`);
* ``scripts/eval/analyze.py`` - raw data -> canonical analysis JSON + tables,
  byte-identical for the same input (:mod:`.analysis`, :mod:`.offline`);
* ``scripts/eval/make_report.py`` - analysis -> Markdown report + figures
  (:mod:`.report`, :mod:`.figures`).

Modules:

* :mod:`.arms` - the baselines B0-B6 and live ablations A5/A6 (D416/D418):
  the placement each forces and the controller profile it needs;
* :mod:`.spec` - the campaign file (YAML) and its validation;
* :mod:`.schedule` - block randomisation (D417), deterministic from the seed;
* :mod:`.raw` - the raw data folder: ``campaign.json`` + append-only
  ``runs.jsonl`` (resume = skip the runs already there);
* :mod:`.fabric` - what the runner needs from a fabric (a protocol) and the
  real implementation over the controller's REST API;
* :mod:`.runner` - the campaign loop;
* :mod:`.measure` - one job's controller record -> the per-job metrics;
* :mod:`.analysis` - live metrics, regret vs oracle, paired bootstrap verdicts
  (reusing ``planner_eval`` and ``bench.stats``);
* :mod:`.offline` - A1-A4 from the offline evaluator / ML comparison reports;
* :mod:`.canonical` - canonical JSON, hashes, atomic writes.

Everything except :mod:`.fabric`'s HTTP class and :mod:`.figures` is pure
(no network, no clock) so the analysis is reproducible from the raw files
alone.
"""

from __future__ import annotations

__all__: list[str] = []
