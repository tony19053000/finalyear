"""``fabric_check restart-check`` against a stub controller whose listening socket
is really closed and re-opened (criterion 3 timing, criterion 4 identity)."""

from __future__ import annotations

from _fabric.api import FabricApi
from _fabric.restart import (
    NO_NODES,
    NODE_ID_CHANGED,
    NOT_ALL_BACK,
    NOT_UP,
    RestartConfig,
    run_restart_check,
)
from _fabric.testing import (
    FakeClock,
    ScriptedOperator,
    StubController,
    node_doc,
    step,
)

A = "node_01JAAAAAAAAAAAAAAAAAAAAAAA"
B = "node_01JBBBBBBBBBBBBBBBBBBBBBBB"
B2 = "node_01JCCCCCCCCCCCCCCCCCCCCCCC"


def _fabric(stub: StubController) -> None:
    stub.nodes[A] = node_doc(A, "lap1", epoch=3)
    stub.nodes[B] = node_doc(B, "lap2", epoch=7)
    stub.add_samples(A, 3, range(1, 51))
    stub.add_samples(B, 7, range(1, 81))


def _kill_and_restart(
    stub: StubController, clock: FakeClock, *, b_back: str | None = B
) -> None:
    """Controller dies 1 s after the prompt and serves again 3 s later (all rows
    disconnected, live rings empty - a new process); A is back 1 s after that,
    B (or a stranger with B's hostname) 2.5 s after."""

    def restart() -> None:
        with stub.lock:
            for node in stub.nodes.values():
                node["connected"] = False
            stub.samples.clear()
        stub.start()

    def a_back() -> None:
        stub.set_node(A, state="online", connected=True, session_epoch=4)
        stub.add_samples(A, 4, [51])

    def b_returns() -> None:
        if b_back == B:
            stub.set_node(B, state="online", connected=True, session_epoch=8)
            stub.add_samples(B, 8, [81])
        elif b_back is not None:
            with stub.lock:
                stub.nodes[b_back] = node_doc(b_back, "lap2", epoch=1)

    clock.after(1.0, stub.stop)
    clock.after(4.0, restart)
    clock.after(5.0, a_back)
    clock.after(6.5, b_returns)


def test_reconnect_is_timed_from_the_new_controller_process(
    stub_controller: StubController,
) -> None:
    _fabric(stub_controller)
    clock = FakeClock()
    kill = step(clock, [(0.0, lambda: _kill_and_restart(stub_controller, clock))], None)
    op = ScriptedOperator(clock, [kill])
    result = run_restart_check(
        FabricApi(stub_controller.url),
        clock,
        op,
        RestartConfig(reps=1, sample_wait_s=1.0),
    )
    assert result.errors == []
    (sample,) = result.samples
    assert 2.9 <= sample["outage_s"] <= 3.2
    assert 2.4 <= sample["reconnect_all_s"] <= 2.6
    assert sample["reconnect_all_within_bound"] is True
    a, b = sample["nodes"][A], sample["nodes"][B]
    assert 0.9 <= a["reconnect_s"] <= 1.1
    assert (a["epoch_before"], a["epoch_after"]) == (3, 4)
    assert a["node_id_kept"] is True and b["node_id_kept"] is True
    # epochs moved, sequences continued: no agent restarted
    assert a["restarted"] is False and b["restarted"] is False
    assert "KILL" in op.prompts[0]


def test_a_node_back_under_a_new_node_id_is_a_criterion_4_failure(
    stub_controller: StubController,
) -> None:
    _fabric(stub_controller)
    clock = FakeClock()
    kill = step(
        clock,
        [(0.0, lambda: _kill_and_restart(stub_controller, clock, b_back=B2))],
        None,
    )
    op = ScriptedOperator(clock, [kill])
    cfg = RestartConfig(reps=1, reconnect_timeout_s=10.0, sample_wait_s=0.5)
    result = run_restart_check(FabricApi(stub_controller.url), clock, op, cfg)
    kinds = {e["failure_type"] for e in result.errors}
    assert kinds == {NOT_ALL_BACK, NODE_ID_CHANGED}
    (sample,) = result.samples
    assert sample["reconnect_all_s"] is None
    assert sample["reconnect_all_within_bound"] is False
    assert sample["nodes"][B]["node_id_kept"] is False
    assert sample["nodes"][B]["new_node_id"] == B2
    assert sample["unexpected_new_nodes"] == [B2]


def test_a_controller_that_never_comes_back_aborts_with_a_typed_failure(
    stub_controller: StubController,
) -> None:
    _fabric(stub_controller)
    clock = FakeClock()
    op = ScriptedOperator(clock, [step(clock, [(0.5, stub_controller.stop)], None)])
    cfg = RestartConfig(reps=2, up_timeout_s=5.0)
    result = run_restart_check(FabricApi(stub_controller.url), clock, op, cfg)
    assert result.aborted
    assert [e["failure_type"] for e in result.errors] == [NOT_UP]


def test_nothing_connected_means_nothing_to_measure(
    stub_controller: StubController,
) -> None:
    stub_controller.nodes[A] = node_doc(A, "lap1", state="offline", connected=False)
    clock = FakeClock()
    op = ScriptedOperator(clock, [])
    result = run_restart_check(
        FabricApi(stub_controller.url), clock, op, RestartConfig(reps=1)
    )
    assert [e["failure_type"] for e in result.errors] == [NO_NODES]
    assert op.prompts == []
