"""Shared helpers for the controller component tests (G2) and the agent component
tests (G3): fast liveness timing and settings builders.

Imported as ``mf_controller_harness`` by ``tests/component/controller/conftest.py``
and by test modules directly - never ``from conftest import ...``: with prepend
import mode the bare ``conftest`` module name is whichever test directory's
conftest pytest loaded last, so that import breaks as soon as two component
directories are collected in one run (surfaced when G3 added
``tests/component/agent``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meshforge.controller.settings import (
    ControllerSection,
    ControllerSettings,
    LivenessSettings,
)

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "FAST_LIVENESS",
    "HELLO_TIMEOUT_S",
    "SHUTDOWN_DRAIN_S",
    "TOLERANT_ADMISSION_TIMEOUT_S",
    "TOLERANT_LIVENESS",
    "controller_section",
    "make_settings",
    "tolerant_liveness",
]

# heartbeat 0.2 s, SUSPECT at 0.25 s, OFFLINE at 0.6 s (x3)
FAST_LIVENESS = LivenessSettings(
    heartbeat_interval_s=0.2, miss_allowance_s=0.05, lease_multiplier=3
)
HELLO_TIMEOUT_S = 0.5
SHUTDOWN_DRAIN_S = 2.0

# For tests whose subject is NOT liveness (jobs, logs, outputs, fencing): the
# agent under test runs in the test's own event loop, so on a loaded machine one
# descheduled half-second is enough to expire FAST_LIVENESS's 0.6 s lease and
# turn a correct run into node_lost. Heartbeat 0.2 s as above (heartbeat-driven
# behaviour stays fast); SUSPECT after 2.2 s, OFFLINE after 10 s of silence.
# A test that wants a node to go SUSPECT/OFFLINE by silence keeps FAST_LIVENESS.
TOLERANT_LIVENESS = LivenessSettings(
    heartbeat_interval_s=0.2, miss_allowance_s=2.0, lease_multiplier=50
)
TOLERANT_ADMISSION_TIMEOUT_S = 8.0
"""Hello admission deadline paired with TOLERANT_LIVENESS (below its 10 s lease):
many Hellos at once are admitted one at a time, slowly under load."""


def tolerant_liveness() -> dict[str, Any]:
    """``controller_section`` overrides for TOLERANT_LIVENESS."""
    return {
        "liveness": TOLERANT_LIVENESS,
        "admission_timeout_s": TOLERANT_ADMISSION_TIMEOUT_S,
    }


def controller_section(**overrides: Any) -> ControllerSection:
    base: dict[str, Any] = {
        "grpc_bind": "127.0.0.1:0",
        "http_bind": "127.0.0.1:0",
        "liveness": FAST_LIVENESS,
        "hello_timeout_s": HELLO_TIMEOUT_S,
        "shutdown_drain_s": SHUTDOWN_DRAIN_S,
        # No mDNS in the controller tests (G3 tests it on purpose, once).
        "discovery": {"advertise": False},
    }
    base.update(overrides)
    return ControllerSection(**base)


def make_settings(
    database: DatabaseSettings, **controller_overrides: Any
) -> ControllerSettings:
    # Keyword arguments beat env/TOML, so a developer's config.toml cannot leak in.
    return ControllerSettings(
        controller=controller_section(**controller_overrides), database=database
    )
