"""The controller's bound on ``Hello.session_epoch`` (ADR-0002 Updates §1, ADR-0013 §6).

``Hello.session_epoch`` is a protobuf ``uint64``. The agent refuses an identity file
whose epoch is outside ``[0, 2^64-1]`` (ADR-0013 §6) and increments the counter
before **every** new stream, so the controller accepts that whole domain: a
tighter bound would strand a legitimate node forever (its epoch only grows) with
no remedy except an identity reset. The registry stores the full domain
(:class:`~meshforge.controller.store.base.UInt64`), so the bound here is the wire
domain, not a storage compromise. Anything outside it can only come from a
non-protobuf caller and is a protocol error (``INVALID_ARGUMENT``), never persisted.
"""

from __future__ import annotations

from typing import Final

from meshforge.controller.store.base import UINT64_MAX

__all__ = ["MAX_SESSION_EPOCH", "epoch_in_range"]

MAX_SESSION_EPOCH: Final = UINT64_MAX
"""Largest ``session_epoch`` the controller admits and stores (``2**64 - 1``)."""


def epoch_in_range(epoch: int) -> bool:
    """True when ``epoch`` is inside ``[0, MAX_SESSION_EPOCH]``."""
    return 0 <= epoch <= MAX_SESSION_EPOCH
