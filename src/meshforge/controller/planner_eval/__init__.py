"""The planner's offline evaluator (V0.14 G4, roadmap 0.14.7; D297-D299, D313-D319).

Given benchmark campaigns (labelled groups, run records, the decisions'
replay snapshots), what would every policy have picked for each group, and how
much slower is that than the best measured configuration? That is criteria 1
and 2 of V0.14, computed the same way every time.

* :mod:`.model` - inputs, settings and policy rows (``oracle`` included, D298);
* :mod:`.mapping` - a planner pick -> the configuration that measured it (D314);
* :mod:`.heldout` - leave-one-family-out calibration refits (D316);
* :mod:`.core` - the pure evaluation: group pick (D313), regret (D315),
  bootstrap CIs and verdicts (D317);
* :mod:`.report` - the canonical JSON text and the Markdown rendering;
* :mod:`.regret_gate` - D319's regret check of a new calibration version;
* :mod:`.loader` - the database reads (labels, records, replays);
* :mod:`.store` - ``planner_eval_report`` (migration 0032, D318);
* :mod:`.service` - load -> evaluate -> store, for the API.

The pure modules (``model``, ``mapping``, ``heldout``, ``core``, ``report``,
``regret_gate``) import only the stdlib, ``meshforge.common``,
``meshforge.bench.stats``, the planner and each other (an AST test pins it).
"""
