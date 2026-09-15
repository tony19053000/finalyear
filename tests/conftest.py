"""Suite-wide pytest policy (roadmap 0.1.8): test tiers, capability markers, gating.

Tier rule: a test's tier is the first directory under ``tests/`` —
``tests/unit/**`` is ``unit``, ``tests/component/**`` is ``component``, and so on.
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
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

TESTS_DIR: Final = Path(__file__).resolve().parent

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


def _tier_of_path(path: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(TESTS_DIR).parts
    except ValueError:
        return None
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
