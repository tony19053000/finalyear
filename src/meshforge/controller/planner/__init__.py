"""The planner (V0.6 G2, D102; ADR-0007): candidates -> feasibility -> estimate ->
policy, as a pure function of ``(request, snapshot, policy, now)``.

* :mod:`.types` - the frozen inputs and outputs;
* :mod:`.feasibility` - static and live rejection rules, slot choice;
* :mod:`.estimator` - predicted completion time (estimator v0, V0.6 G4);
* :mod:`.policies` - the seven ranking policies (D107);
* :mod:`.plan` - the pipeline;
* :mod:`.retry_exclusion` - which nodes a retry avoids (V0.6 G3, D110);
* :mod:`.decision_json` - a :class:`~.types.Decision` as the JSON rows of
  ``plan_decision.candidates``.

No database, no asyncio, no controller state: the dispatcher builds the
snapshot (``controller/jobs/planning.py``) and stores the decision.
"""

from meshforge.controller.planner.plan import candidates, plan, snapshot_digest
from meshforge.controller.planner.retry_exclusion import PastFailure, exclusions
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Estimate,
    EstimateTerm,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    Rejection,
    TermSource,
    TreeNeed,
)

__all__ = [
    "CandidateEval",
    "ClusterSnapshot",
    "Decision",
    "Estimate",
    "EstimateTerm",
    "History",
    "NodeSnapshot",
    "PastFailure",
    "PlanRequest",
    "PlannerConfig",
    "RejectCode",
    "Rejection",
    "TermSource",
    "TreeNeed",
    "candidates",
    "exclusions",
    "plan",
    "snapshot_digest",
]
