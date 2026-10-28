"""G7-A (0.3.4, D32b): THE HEARTBEAT TEST.

A real controller (Postgres, fast liveness: heartbeat 0.2 s, OFFLINE after 0.6 s)
and a real agent session run in this process while a real all-threads CPU
benchmark (numpy in the benchmark child, every core) saturates the machine. The
node must never leave ONLINE - on the controller's live view, in the registry, or
on the agent's side - and the stream must never drop.

Run with the child's priority lowered (the product default) **and** without it, so
the handoff can say whether the priority drop is what keeps the node ONLINE. The
``no-nice`` case is ``perf`` (D422): on a shared CI runner it measures the
runner's load, so it runs on a quiet lab laptop (``docs/LAB.md``
"Load-sensitive timing tests"); the product-default ``nice10`` case is the gate.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest
from mf_agent_harness import AgentFactory

from meshforge.agent.benchmarks.model import MetricName, MetricStatus
from meshforge.agent.benchmarks.plan import Targets
from meshforge.agent.benchmarks.runner import HostFacts, RunnerConfig, ScoreRunner
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.state import AgentState
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime

pytestmark = [
    pytest.mark.timeout(240),
    pytest.mark.skipif(
        importlib.util.find_spec("numpy") is None,
        reason="numpy is not installed (uv sync --extra agent): no all-threads load",
    ),
]

ONLINE_TIMEOUT_S = 15.0
SAMPLE_S = 0.02
#: Long enough per repetition that the all-threads cases load the machine for
#: several seconds - many OFFLINE windows (0.6 s) back to back.
LOAD = Targets(cpu_target_s=0.2, mem_target_s=0.1, gpu_target_s=0.1)


async def _no_conditions() -> tuple[bool | None, float | None]:
    return None, None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lower_priority",
    [
        pytest.param(True, id="nice10"),
        # D422: without the priority drop, staying ONLINE depends on how busy
        # the host is - a measurement for a quiet lab laptop, not a CI gate.
        pytest.param(False, id="no-nice", marks=pytest.mark.perf),
    ],
)
async def test_node_stays_online_while_an_all_threads_benchmark_runs(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
    lower_priority: bool,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    node_id = str(identity.node_id)
    assert runtime.manager is not None
    threads = os.cpu_count() or 1
    runner = ScoreRunner(
        RunnerConfig(
            budget_s=120,
            case_timeout_s=60,
            targets=LOAD,
            lower_priority=lower_priority,
            extra_env={"MESHFORGE_BENCH_DISABLE": "torch"},
        ),
        HostFacts(cpu_threads=threads),
        fingerprint="e" * 64,
        conditions=_no_conditions,
    )

    bench = asyncio.create_task(runner.run())
    leaves: list[str] = []
    samples = 0
    loop = asyncio.get_running_loop()
    started = loop.time()
    last = started
    max_stall = 0.0  # the longest the event loop went without running us
    while not bench.done():
        now = loop.time()
        max_stall = max(max_stall, now - last - SAMPLE_S)
        last = now
        live = runtime.manager.get(node_id)
        if live is None or live.state is not NodeState.ONLINE:
            leaves.append(f"controller:{None if live is None else live.state}")
        if running.client.state is not AgentState.ONLINE:
            leaves.append(f"agent:{running.client.state}")
        samples += 1
        await asyncio.sleep(SAMPLE_S)
    load_s = loop.time() - started
    scores = bench.result()

    all_threads = scores.metric(MetricName.CPU_FP32_GFLOPS_ALL)
    assert all_threads is not None and all_threads.status is MetricStatus.OK
    assert all_threads.threads_effective == threads  # really every core
    assert scores.conditions.priority_lowered is lower_priority
    assert load_s > 3 * 0.6, f"the load lasted only {load_s:.2f} s"
    assert samples > 50
    assert leaves == [], (
        f"the node left ONLINE under load: {leaves[:5]}; "
        f"longest event-loop stall {max_stall:.3f} s"
    )
    assert running.client.stats.stream_ends == []
    node = await registry.get_node(node_id)
    assert node is not None and node.state is NodeState.ONLINE
