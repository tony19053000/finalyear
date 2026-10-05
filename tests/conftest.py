"""Suite-wide pytest policy (roadmap 0.1.8): test tiers, capability markers, gating.

Tier rule: a test's tier is the first directory under ``tests/`` —
``tests/unit/**`` is ``unit``, ``tests/component/**`` is ``component``, and so on;
the one nested exception is ``tests/cluster/chaos/**``, which is ``chaos``
(:data:`NESTED_TIERS`: it shares the cluster fixtures but is opt-in).
The marker is applied automatically, so ``pytest -m unit`` works even for a file
that forgot ``pytestmark``. Collection *fails* (not warns) when:

* a test file lives outside a tier directory (e.g. directly in ``tests/``), or
* a test carries a tier marker that differs from its directory, or more than one.

Directory-derived tiers were chosen over "every file must declare pytestmark"
because the file location is the one thing an agent cannot forget to set and a
reviewer can check at a glance; an explicit ``pytestmark`` is still allowed and is
cross-checked, so a copy-pasted ``pytest.mark.unit`` in ``tests/component`` fails.

Tiers (= directories): ``unit``, ``component``, ``cluster``, ``lab``, ``e2e``,
``soak``, ``chaos``. Opt-in tiers (``lab``, ``soak``, ``chaos``) are skipped unless
enabled with ``--run-<tier>``, so a bare ``pytest`` on any laptop or CI runner never
fails for missing hardware, humans, or time.

Capability markers say what a test *needs*, orthogonal to its tier, so e.g. a Mode 2
Remote GPU test can be ``component`` + ``gpu`` and a Mode 4 test ``cluster`` + ``gpu``:

* ``gpu`` — skipped unless ``--run-gpu`` is given, then also skipped (with the
  reason) when torch is missing or reports no CUDA device.
* ``docker`` (or the ``docker_available`` fixture, used by the shared Postgres
  fixtures) — skipped with the reason when the ``docker`` Python package is not
  installed or no Docker daemon answers (e.g. Windows CI runners without Linux
  Docker); the two cases have distinct reasons.
* ``slow`` — skipped unless ``--run-slow`` (minutes of wall time, network or cold
  caches; e.g. the fresh-clone ``uv sync`` test). Combines with any tier, so
  ``pytest -m component --run-slow`` runs component tests including slow ones.

Environment variables read by the suite:

* ``MESHFORGE_FRESH_CLONE_CHILD=1`` — set *only* by
  ``tests/component/test_fresh_clone_unit_criterion.py`` on the pytest runs it
  launches inside its temporary clone, so that module skips itself there instead
  of recursing (clone -> clone -> ...). Never set it by hand: it silently disables
  the fresh-clone test.
* ``MESHFORGE_TEST_RUN_ID`` — value of the ``meshforge.test.run-id`` label on the
  test containers this run starts (default: random per process). Set by
  ``tests/component/test_container_cleanup.py`` on the child pytest runs it
  launches, to find exactly their containers; see ``tests/component/conftest.py``.

Shared test helpers live in ``tests/_support/`` (no test files); this conftest puts
that directory on ``sys.path`` so they import as top-level modules (e.g.
``mf_test_containers``), matching ``mypy_path`` in ``pyproject.toml``.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from meshforge.common.logging import configure_logging

TESTS_DIR: Final = Path(__file__).resolve().parent

_SUPPORT_DIR: Final = str(TESTS_DIR / "_support")
if _SUPPORT_DIR not in sys.path:
    sys.path.insert(0, _SUPPORT_DIR)

#: Every tier marker; each is also the name of its directory under tests/.
TIERS: Final = ("unit", "component", "cluster", "lab", "e2e", "soak", "chaos")

#: Capability markers: what a test needs, combined with exactly one tier.
CAPABILITY_MARKERS: Final = ("docker", "gpu", "slow")

#: Tiers/markers that only run when explicitly requested with ``--run-<name>``
#: (hardware, humans, or long runtimes). ``gpu``/``slow`` are capabilities, not tiers.
OPT_IN: Final = {
    "lab": "needs real laptops on the LAN (human-run lab step)",
    "soak": "long-running soak test",
    "chaos": "long-running chaos test",
    "gpu": "needs an NVIDIA GPU with CUDA",
    "slow": "slow test (minutes; network or cold caches)",
}

_DOCKER_REASON_KEY: Final = pytest.StashKey[str | None]()
_CUDA_REASON_KEY: Final = pytest.StashKey[str | None]()
_DOCKER_PING_TIMEOUT_S: Final = 5


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("meshforge", "MeshForge test tiers and capabilities")
    for name, why in OPT_IN.items():
        group.addoption(
            f"--run-{name}",
            action="store_true",
            default=False,
            help=f"run `{name}` tests ({why}); skipped by default",
        )


#: Tier directories nested inside another tier's directory (V0.6 G3, D118): the
#: chaos suite lives in ``tests/cluster/chaos/`` to share the cluster harness and
#: fixtures, but is its own opt-in tier.
NESTED_TIERS: Final = {("cluster", "chaos"): "chaos"}


def _tier_of_path(path: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(TESTS_DIR).parts
    except ValueError:
        return None
    if len(parts) > 2 and (parts[0], parts[1]) in NESTED_TIERS:
        return NESTED_TIERS[(parts[0], parts[1])]
    return parts[0] if len(parts) > 1 and parts[0] in TIERS else None


@pytest.hookimpl(tryfirst=True)  # before `-m` deselection sees the markers
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    problems: list[str] = []
    for item in items:
        tier = _tier_of_path(item.path)
        declared = sorted({m.name for m in item.iter_markers() if m.name in TIERS})
        if tier is None:
            problems.append(
                f"{item.nodeid}: not inside a tier directory "
                f"(tests/<{'|'.join(TIERS)}>/...)"
            )
            continue
        if declared and declared != [tier]:
            problems.append(
                f"{item.nodeid}: tier markers {declared} do not match directory "
                f"tier '{tier}' (exactly one tier marker, equal to the directory)"
            )
            continue
        if not declared:
            item.add_marker(getattr(pytest.mark, tier))
    if problems:
        raise pytest.UsageError(
            "test tier policy violated (see tests/conftest.py):\n  "
            + "\n  ".join(problems)
        )


def _probe_docker() -> str | None:
    try:
        import docker
    except ImportError as exc:
        return (
            "Docker tests skipped: the `docker` Python package is not installed "
            f"(it comes with the dev dependency group: `uv sync`) ({exc})"
        )
    try:
        client = docker.from_env(timeout=_DOCKER_PING_TIMEOUT_S)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - any failure means "no containers here"
        return (
            "Docker is not available: no Docker daemon answered "
            f"({type(exc).__name__}: {exc})"
        )
    return None


def docker_unavailable_reason(config: pytest.Config) -> str | None:
    """``None`` if a Docker daemon answers a ping, else a human-readable reason.

    Checked once per session and only when a Docker-needing test is about to run.
    A missing ``docker`` package and an unreachable daemon have distinct reasons.
    """
    if _DOCKER_REASON_KEY not in config.stash:
        config.stash[_DOCKER_REASON_KEY] = _probe_docker()
    return config.stash[_DOCKER_REASON_KEY]


def _cuda_unavailable_reason(config: pytest.Config) -> str | None:
    if _CUDA_REASON_KEY not in config.stash:
        reason: str | None = None
        try:
            import torch

            if not torch.cuda.is_available():
                reason = "--run-gpu given but torch reports no CUDA device"
        except ImportError:
            reason = "--run-gpu given but torch is not installed (uv sync --extra gpu)"
        config.stash[_CUDA_REASON_KEY] = reason
    return config.stash[_CUDA_REASON_KEY]


def pytest_runtest_setup(item: pytest.Item) -> None:
    config = item.config
    for name, why in OPT_IN.items():
        if item.get_closest_marker(name) and not config.getoption(f"--run-{name}"):
            pytest.skip(f"{name} test skipped: {why}; pass --run-{name} to run it")
    if item.get_closest_marker("gpu"):
        reason = _cuda_unavailable_reason(config)
        if reason:
            pytest.skip(reason)
    if item.get_closest_marker("docker"):
        reason = docker_unavailable_reason(config)
        if reason:
            pytest.skip(reason)


@pytest.fixture(scope="session")
def docker_available(pytestconfig: pytest.Config) -> None:
    """Depend on this fixture to skip (with the reason) when Docker is unreachable."""
    reason = docker_unavailable_reason(pytestconfig)
    if reason:
        pytest.skip(reason)


@pytest.fixture(autouse=True)
def _configured_logging() -> None:
    """Give the whole suite the logging configuration the real processes use.

    Not cosmetic — it removes a way to wedge the suite. structlog's *default*
    (unconfigured) processor chain renders a rich traceback whose cost grows
    superlinearly with the exception message: a V0.3 G5 tester test that logged a
    200 KB exception through it spun at 100% CPU for minutes and had to be
    deleted. :func:`~meshforge.common.logging.configure_logging` installs
    ``ExceptionDictTransformer(use_rich=False)`` and caps scrubbed text, so the
    same call is ~1.5 ms and flat in the message length.

    Configuring it here makes that hazard structurally impossible rather than
    something each new test has to remember. Output goes to an in-memory stream so
    log lines never pollute pytest's captured output; a test that wants to *read*
    what was logged still uses ``structlog.testing.capture_logs()``, which
    replaces the chain for its own block.

    **Function-scoped, not session-scoped, and that is the whole point.**
    ``tests/unit/common/test_common_logging*.py`` legitimately call
    ``structlog.reset_defaults()`` in their own fixtures, so a once-per-session
    call leaves every *later* test running on the default chain again - which is
    exactly how the full suite came to spin for minutes at ~90% CPU inside
    ``rich/text.py`` while each file passed in under a second on its own. (The
    spin holds the GIL, so ``--timeout`` never fires: the hang looks like a stall
    with no failing test, which is what made it expensive to find.) Re-applying
    per test is ~0.2 ms and restores the invariant no matter what ran before.
    Tests that want a different configuration simply call ``configure_logging``
    themselves afterwards, as those logging tests already do.
    """
    configure_logging(fmt="json", stream=io.StringIO())


def half_imported_packages(modules: Mapping[str, object]) -> list[str]:
    """Top-level packages that have a submodule loaded but whose parent is missing.

    That is the fingerprint of an import that raised half-way and was swallowed:
    Python drops the failed package from ``sys.modules`` but keeps whatever it had
    already imported beneath it - for a native package, a loaded C extension.
    """
    return sorted(
        {
            name.partition(".")[0]
            for name, module in modules.items()
            if module is not None
            and "." in name
            and modules.get(name.rpartition(".")[0]) is None
        }
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run if a test left a package half-imported in this process.

    Not hypothetical: the S7 child tests used to import the real torch in-process,
    ``filterwarnings = error`` made that import raise half-way, and repeated
    half-imports corrupted torch's C extension - the suite printed ``3214
    passed`` and then segfaulted at shutdown (exit 139) with no Python frame to
    point at the cause. This check runs *before* shutdown and names the package,
    so the same mistake fails with a reason instead of a crash. A test that needs
    an optional library to look missing should block it with
    ``monkeypatch.setitem(sys.modules, name, None)``, not import it and swallow
    the error. ``scripts/pytest_exit_guard.py`` is the other half: it catches a
    shutdown crash this check cannot foresee.
    """
    packages = half_imported_packages(dict(sys.modules))
    if not packages:
        return
    message = (
        f"half-imported package(s) left in the pytest process: {', '.join(packages)}. "
        "A test imported them, the import raised part-way and the error was "
        "swallowed; a half-loaded native extension can crash the interpreter at "
        "exit. Block the import in the test instead."
    )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "half-imported packages", red=True)
        reporter.write_line(message, red=True)
    else:
        sys.stderr.write(message + "\n")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
