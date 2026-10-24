"""When the setup page applies (V0.17 G2, D392). Pure.

=================  ==========  ===========  ========================================
fabric CA          users       setup token  state
=================  ==========  ===========  ========================================
present            any         any          ``done`` (410 forever)
absent             any exists  any          ``done`` (410; ``fabric create`` by CLI)
absent             none        present      ``pending`` (the setup page is served)
absent             none        absent       ``unavailable`` (mtls refuses to start:
                                            run ``setup url`` or ``fabric create``)
=================  ==========  ===========  ========================================
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SetupState", "setup_state"]


class SetupState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    UNAVAILABLE = "unavailable"


def setup_state(
    *, ca_present: bool, users_exist: bool, token_present: bool
) -> SetupState:
    if ca_present or users_exist:
        return SetupState.DONE
    if token_present:
        return SetupState.PENDING
    return SetupState.UNAVAILABLE
