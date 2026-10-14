"""V0.7 G7 tester r1 (criterion 3): which planes does the D146 recorder cover?

Criterion 3 reads "no plaintext job data or credentials on **any plane**". The
controller listens on four ports (session 47100, enrol 47101, REST + blobs 47110)
and every agent on one (peer 47120). The G7 recorder run puts a recording link
in front of all of them except the enrolment listener, so a join code redeemed
over 47101 is never captured.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric

pytestmark = [pytest.mark.cluster, pytest.mark.security, pytest.mark.timeout(240)]


def test_control_every_other_plane_has_a_recording_link(
    make_fabric: Callable[..., Fabric],
) -> None:
    fabric = make_fabric(
        agents=2,
        record_links=True,
        agent_extra={"peer": {"bind": "127.0.0.1", "port": 0}},
    )
    names = {link.name for link in fabric.links()}
    assert {"rest", "agent0", "agent1", "agent0-peer", "agent1-peer"} <= names


@pytest.mark.xfail(
    strict=True,
    reason="TQ-5: the enrolment listener (47101) has no recording link, so the "
    "criterion 3 run never captures a join code being redeemed",
)
def test_the_enrolment_plane_is_recorded_too(make_fabric: Callable[..., Fabric]) -> None:
    fabric = make_fabric(
        agents=1,
        record_links=True,
        agent_extra={"peer": {"bind": "127.0.0.1", "port": 0}},
    )
    assert fabric.enroll_target, "no enrolment listener"
    names = {link.name for link in fabric.links()}
    assert any("enroll" in name for name in names), names
