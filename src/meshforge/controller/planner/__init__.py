"""The planner (V0.6 G2, D102; ADR-0007): candidates -> feasibility -> estimate ->
policy, as a pure function of ``(request, snapshot, policy, now)``.

* :mod:`.types` - the frozen inputs and outputs;
* :mod:`.candidates` - stage 1, the one candidate generator for every
  request kind (V0.14 G1, D291);
* :mod:`.feasibility` - static and live rejection rules, slot choice;
* :mod:`.estimator` - predicted completion time (estimator v0, V0.6 G4);
* :mod:`.policies` - the seven ranking policies (D107);
* :mod:`.plan` - the pipeline;
* :mod:`.split` / :mod:`.split_plan` - Mode 3: item rates, chunk size, node
  sets, the "don't split" margin test (V0.10 G3, D265);
* :mod:`.retry_exclusion` - which nodes a retry avoids (V0.6 G3, D110);
* :mod:`.decision_json` - a :class:`~.types.Decision` as the JSON rows of
  ``plan_decision.candidates``;
* :mod:`.replay` - a decision's inputs as a bounded, versioned JSON replay
  snapshot that :func:`plan` reproduces exactly (V0.14 G1, D292).

No database, no asyncio, no controller state: the dispatcher builds the
snapshot (``controller/jobs/planning.py``) and stores the decision.
"""

from meshforge.controller.planner.candidates import (
    Candidate,
    CandidateSet,
    candidates,
    request_kind,
)
from meshforge.controller.planner.plan import plan, snapshot_digest
from meshforge.controller.planner.retry_exclusion import PastFailure, exclusions
from meshforge.controller.planner.types import (
    AcceleratorRequest,
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Estimate,
    EstimateTerm,
    History,
    LanePlan,
    MarginTest,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    Rejection,
    RequestKind,
    SplitConfig,
    SplitInfo,
    SplitPlan,
    TermSource,
    TreeNeed,
)

__all__ = [
    "AcceleratorRequest",
    "Candidate",
    "CandidateEval",
    "CandidateSet",
    "ClusterSnapshot",
    "Decision",
    "Estimate",
    "EstimateTerm",
    "History",
    "LanePlan",
    "MarginTest",
    "NodeSnapshot",
    "PastFailure",
    "PlanRequest",
    "PlannerConfig",
    "RejectCode",
    "Rejection",
    "RequestKind",
    "SplitConfig",
    "SplitInfo",
    "SplitPlan",
    "TermSource",
    "TreeNeed",
    "candidates",
    "exclusions",
    "plan",
    "request_kind",
    "snapshot_digest",
]
