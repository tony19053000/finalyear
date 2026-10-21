"""Cluster-tier fixtures (roadmap 0.3.14): a real controller + N real agents as processes.

The Postgres fixtures are the component tier's own (``tests/_support/mf_postgres.py``,
imported here so pytest registers them for this directory): one container per
session, a fresh database per test. :func:`make_fabric` starts a
:class:`mf_cluster.Fabric` on that database and, after the test, runs the
invariant check every scenario must pass (no forbidden events, no tracebacks, no
unrequested restarts, every expected process alive) and tears the fabric down,
asserting that no process survived.

Scenario modules ``pytest.importorskip`` the agent/controller extras before
importing ``mf_cluster``, so a base install (plain ``uv sync``) skips them with a
reason instead of failing collection; the proxy tests need neither.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from mf_postgres import pg_settings, postgres_server  # noqa: F401

if TYPE_CHECKING:
    from mf_cluster import Fabric

    from meshforge.controller.store.settings import DatabaseSettings


@dataclasses.dataclass(frozen=True)
class TimingRow:
    scenario: str
    measured_s: float
    bound_s: float | None
    profile: str


_TIMINGS: Final = pytest.StashKey[list[TimingRow]]()

RecordTiming = Callable[[str, float, "float | None", str], None]
"""``record_timing(scenario, measured_s, bound_s, profile)``."""


@pytest.fixture
def record_timing(
    pytestconfig: pytest.Config, record_property: Callable[[str, object], None]
) -> RecordTiming:
    """Collect one measured timing for the end-of-run table (and the junit XML).

    Every number is a **loopback** number: controller, agents and proxy share one
    machine, so it bounds the software, never a real network (S3's lesson).
    """

    def record(
        scenario: str, measured_s: float, bound_s: float | None, profile: str
    ) -> None:
        pytestconfig.stash.setdefault(_TIMINGS, []).append(
            TimingRow(scenario, measured_s, bound_s, profile)
        )
        record_property(f"timing:{scenario}", round(measured_s, 3))

    return record


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    rows = config.stash.get(_TIMINGS, [])
    if not rows:
        return
    terminalreporter.write_sep("=", "cluster timings (LOOPBACK, one machine)")
    width = max(len(r.scenario) for r in rows)
    terminalreporter.write_line(
        f"{'scenario':<{width}}  {'profile':<8} {'measured s':>10} {'bound s':>8}"
    )
    for r in rows:
        bound = "-" if r.bound_s is None else f"{r.bound_s:.2f}"
        terminalreporter.write_line(
            f"{r.scenario:<{width}}  {r.profile:<8} {r.measured_s:>10.3f} {bound:>8}"
        )


@pytest.fixture
def make_fabric(
    pg_settings: DatabaseSettings,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> Iterator[Callable[..., Fabric]]:
    """``make_fabric(agents=5, timing=FAST, ...)`` -> a started :class:`Fabric`."""
    pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
    pytest.importorskip(
        "sqlalchemy", reason="the controller needs the controller extra"
    )
    from mf_cluster import Fabric

    made: list[Fabric] = []

    def make(**kwargs: Any) -> Fabric:
        fabric = Fabric(pg_settings, tmp_path / f"fabric{len(made)}", **kwargs)
        made.append(fabric)
        fabric.start()
        return fabric

    yield make
    errors: list[BaseException] = []
    for fabric in made:
        # Invariants first (they read the logs of processes that must still be
        # alive), then teardown - which runs even if the invariants failed.
        for step in (fabric.check_invariants, fabric.close):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - report every fabric's failure
                errors.append(exc)
    if errors:
        raise errors[0]


@pytest.fixture(scope="session")
def numpy_wheelhouse() -> Path:
    """A local folder holding exactly the benchmark jobs' pinned numpy wheel
    for this interpreter (V0.10 TD1; ``tests/_support/mf_wheelhouse.py``):
    from the cache, else downloaded once (URL + sha256 from ``uv.lock``). Pass
    it to ``mf_split_cluster.split_fabric(wheelhouse=...)`` so the agents
    install offline. Skips with the reason when there is neither a cached
    wheel nor a network; **fails** when a fresh download is not the locked
    wheel (V0.10 TD2)."""
    from mf_wheelhouse import (
        WheelDownloadMismatchError,
        WheelhouseUnavailableError,
        ensure_wheel,
        locked_wheels,
        pick_wheel,
    )

    from meshforge.bench.workloads.materialise import NUMPY_REQUIREMENT

    name, _, version = NUMPY_REQUIREMENT.partition("==")
    wheel = pick_wheel(locked_wheels(name, version))
    if wheel is None:
        pytest.skip(f"uv.lock has no {NUMPY_REQUIREMENT} wheel for this interpreter")
    try:
        return ensure_wheel(wheel)
    except WheelhouseUnavailableError as exc:
        pytest.skip(str(exc))
    except WheelDownloadMismatchError as exc:  # V0.10 TD2: never a skip
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(autouse=True)
def _trust_the_test_fabric(monkeypatch: pytest.MonkeyPatch) -> None:
    """V0.7 G3: every controller here serves HTTPS with a certificate from the
    test process's fabric CA (``mf_fabric``); plain ``httpx``/``urllib``
    clients trust it through ``SSL_CERT_FILE`` (standard host-name check -
    the server certificates name 127.0.0.1). Nothing is mocked."""
    import mf_fabric

    monkeypatch.setenv("SSL_CERT_FILE", str(mf_fabric.ca_file()))
