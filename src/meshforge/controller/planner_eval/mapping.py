"""A planner pick -> the campaign configuration that measured it (D314).

Regret is only honest if this mapping is exact and auditable, so it is one
small pure function with a test per case:

==========================================  =====================================
the decision chose                          the configuration
==========================================  =====================================
Local (the submitter's node)                ``local`` on that node, same origin
Mode 1 on node ``n``                        ``mode1`` on ``n``, same origin
Mode 3 over the active lanes ``S``          ``mode3`` with ``node_set == sorted(S)``
                                            (a campaign stores its sets sorted)
Mode 3 "don't split" (D269, one lane)       the ``local`` / ``mode1`` configuration
                                            of that lane's node
nothing (no feasible node)                  none -> ``unmeasured``
==========================================  =====================================

The origin is the record's own configuration's origin (the submitter of the
run). A configuration stored ``unsupported`` never ran, so it never matches.
A pick that matches no configuration is ``unmeasured`` (D297): reported, never
counted as regret 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from meshforge.controller.planner.types import Decision
from meshforge.controller.planner_eval.model import ConfigRef

__all__ = ["Pick", "config_for", "pick_of"]


@dataclass(frozen=True, slots=True)
class Pick:
    """What a decision would run, in campaign terms."""

    strategy: Literal["local", "mode1", "mode3"]
    node_id: str
    """The node (``mode3``: the first lane)."""
    node_set: tuple[str, ...] = ()
    """``mode3`` only: the active lanes' nodes, sorted."""


def pick_of(decision: Decision) -> Pick | None:
    """The pick of ``decision``; None = it chose nothing."""
    chosen = decision.chosen
    if chosen is None:
        return None
    split = decision.split
    if split is not None and split.split and split.lanes:
        if len(split.lanes) > 1:
            return Pick("mode3", split.node_ids[0], tuple(sorted(split.node_ids)))
        # a set with one active lane runs on that node alone (the lane path)
        lane = split.lanes[0]
        return Pick("local" if lane.local else "mode1", lane.node_id)
    if chosen.mode == "local":
        return Pick("local", chosen.node_id)
    # Mode 1, or a split job's one lane on another node (D269 "don't split")
    return Pick("mode1", chosen.node_id)


def config_for(
    pick: Pick | None, origin_node_id: str, configs: Sequence[ConfigRef]
) -> ConfigRef | None:
    """The measured configuration of ``configs`` that ran ``pick`` from
    ``origin_node_id`` (module docstring); None = ``unmeasured``."""
    if pick is None:
        return None
    for config in configs:
        if config.state != "measured" or config.origin_node_id != origin_node_id:
            continue
        if config.strategy != pick.strategy:
            continue
        if pick.strategy == "mode3":
            if tuple(sorted(config.node_set)) == pick.node_set:
                return config
        elif config.node_id == pick.node_id:
            return config
    return None
