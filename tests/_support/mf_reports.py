"""Applying agent reports in store-level tests (V0.7 G7).

``ExecutionStore.apply_status`` takes the reporting node (CG-E2: only the node
an attempt is placed on may move it). Tests about states, retries and fencing
report as that node; tests about the node check pass ``node_id`` themselves.
"""

from __future__ import annotations

from meshforge.controller.jobs.execution_store import (
    ExecutionStore,
    StatusOutcome,
    StatusReport,
)

#: The reporter used for a report about an attempt that does not exist.
NO_SUCH_NODE = "node-of-no-attempt"


async def apply_as_placed(store: ExecutionStore, report: StatusReport) -> StatusOutcome:
    """``store.apply_status(report)`` reported by the attempt's own node."""
    attempt = await store.get_attempt(report.attempt_id)
    node_id = NO_SUCH_NODE if attempt is None else attempt.node_id
    return await store.apply_status(report, node_id=node_id)
