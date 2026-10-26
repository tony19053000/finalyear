"""Facts about the installed Windows agent service that ``common`` code needs (V0.17 G4).

The service itself lives in :mod:`meshforge.agent.winservice` (D386); ``common``
cannot import the agent, yet two common modules must know *who* the service is:

* :mod:`meshforge.common.private_files` - on the installed system (D385,
  ``MESHFORGE_INSTALL_MODE=system``) a private file must stay readable by the
  **service account** whoever wrote it (the service, or an administrator's
  ``meshforge-agent`` command, Q32), see :func:`windows_private_sids`;
* the admin CLI (:mod:`meshforge.common.system_cli`) - detecting the install.

Pure and stdlib-only (it is imported before every command): the service SID is
computed, never looked up, so the result does not depend on the system language.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Mapping
from typing import Final

__all__ = [
    "INSTALL_MODE_ENV_VAR",
    "SERVICE_ACCOUNT",
    "SERVICE_NAME",
    "SID_ADMINISTRATORS",
    "SID_SYSTEM",
    "service_sid",
    "system_mode",
    "windows_private_sids",
]

SERVICE_NAME: Final = "MeshForgeAgent"
SERVICE_ACCOUNT: Final = rf"NT SERVICE\{SERVICE_NAME}"
"""The virtual account the service runs as (D386): no password, no rights beyond
what the installer grants."""
SID_SYSTEM: Final = "S-1-5-18"
SID_ADMINISTRATORS: Final = "S-1-5-32-544"
INSTALL_MODE_ENV_VAR: Final = "MESHFORGE_INSTALL_MODE"
"""Pinned equal to ``common.config``'s by a unit test (no pydantic import here)."""


def service_sid(name: str = SERVICE_NAME) -> str:
    """The per-service SID ``NT SERVICE\\<name>``: ``S-1-5-80-`` + the SHA-1 of the
    upper-cased name in UTF-16LE, as five little-endian 32-bit sub-authorities
    (the documented derivation; checked against ``sc showsid`` in SP1)."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()  # noqa: S324
    subs = struct.unpack("<5I", digest)
    return "S-1-5-80-" + "-".join(str(s) for s in subs)


def system_mode(environ: Mapping[str, str] | None = None) -> bool:
    """``MESHFORGE_INSTALL_MODE=system`` (set by the service, or by the admin CLI
    acting on the service's state). Same parsing as ``common.config.install_mode``
    minus its error for a bad value (that is reported where settings load)."""
    env = os.environ if environ is None else environ
    return env.get(INSTALL_MODE_ENV_VAR, "").strip().lower() == "system"


def windows_private_sids(current_sid: str, *, system: bool) -> tuple[str, ...]:
    """Who may use a private file on Windows.

    * user mode (a developer's agent, ADR-0013 section 5): the current user only;
    * system mode (the installed service, D385): the **service SID, SYSTEM and
      Administrators**, whoever wrote the file. An administrator's elevated
      ``meshforge-agent identity show`` / ``policy set`` (Q32) therefore never
      locks the service out of its own identity or policy, and the service never
      locks the administrators out. This is the Windows analogue of root on the
      Linux package (root reads ``0600`` files of ``meshforge``); Administrators
      can take ownership of any file anyway, so an owner-only key would not keep
      them out. Other local users get nothing.
    """
    if not system:
        return (current_sid,)
    return (service_sid(), SID_SYSTEM, SID_ADMINISTRATORS)
