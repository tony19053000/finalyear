"""From an invite to the join code a human copies (V0.7 G2, D135/D150).

The code needs the **enrolment listener's address as another laptop reaches it**
and the CA pin. The address, in order of precedence:

1. ``--address HOST:PORT`` given to the command;
2. ``[controller.enroll] advertise``;
3. the enrolment bind host, unless it is a wildcard (``0.0.0.0`` / ``::``), and
   the listener's port - the **bound** port when the controller is running (the
   REST ``invite``), the configured one otherwise (``meshforge-controller fabric
   invite`` on the controller host). Port ``0`` (tests) or a wildcard host with
   no advertise is a typed ``config_error`` that says what to set.

The pin is the CA's 128-bit short fingerprint (:func:`pki.short_fingerprint`).
"""

from __future__ import annotations

from dataclasses import dataclass

from meshforge.common import join_code, pki
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.netaddr import parse_bind
from meshforge.controller.enrollment.invites import NewInvite
from meshforge.controller.fabric_ca import FabricCa
from meshforge.controller.settings import ControllerSettings, split_host_port

__all__ = ["CodeAddressError", "IssuedCode", "code_address", "issue_code"]

_WILDCARDS = frozenset({"0.0.0.0", "::"})  # noqa: S104 - recognising wildcards


class CodeAddressError(MeshForgeError):
    """The controller cannot tell which address to put in a join code."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """What ``invite`` shows: the code (secret, shown once) and its facts."""

    code: str
    invite_id: str
    address: str
    ca_pin: str
    expires_at: str
    replace_node_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "invite_id": self.invite_id,
            "address": self.address,
            "ca_pin": self.ca_pin,
            "expires_at": self.expires_at,
            "replace_node_id": self.replace_node_id,
        }


def code_address(
    settings: ControllerSettings,
    *,
    override: str | None = None,
    bound_port: int | None = None,
) -> tuple[str, int]:
    """``(host, port)`` for join codes (see module docstring)."""
    enroll = settings.controller.enroll
    chosen = override or enroll.advertise
    if chosen:
        try:
            return split_host_port(chosen)
        except ValueError as exc:
            raise CodeAddressError(
                str(exc), details={"reason": "bad_address"}
            ) from None
    bind = parse_bind(settings.controller.enroll_bind, allow_any=True)
    port = bound_port if bound_port else bind.port
    if bind.host in _WILDCARDS:
        raise CodeAddressError(
            f"the enrolment listener binds {bind.host}; set [controller.enroll] "
            "advertise = 'LAN-IP:port' (or pass --address) so a code names an "
            "address other laptops can reach",
            details={"reason": "wildcard_bind"},
        )
    if port == 0:
        raise CodeAddressError(
            "the enrolment listener's port is chosen at start (bind port 0); pass "
            "--address HOST:PORT, or use `compute fabric invite` against the "
            "running controller",
            details={"reason": "port_zero"},
        )
    return bind.host, port


def issue_code(new: NewInvite, *, fabric: FabricCa, host: str, port: int) -> IssuedCode:
    """Encode ``new`` as a join code for ``host:port`` pinned to ``fabric``'s CA."""
    code = join_code.JoinCode(
        host=host,
        port=port,
        token=new.token,
        ca_pin=pki.short_fingerprint(fabric.cert),
    )
    text = join_code.encode(code)
    return IssuedCode(
        code=text,
        invite_id=new.record.id,
        address=code.address,
        ca_pin=code.ca_pin,
        expires_at=new.record.expires_at.isoformat(),
        replace_node_id=new.record.replace_node_id,
    )
