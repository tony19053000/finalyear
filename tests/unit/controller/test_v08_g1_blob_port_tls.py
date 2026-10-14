"""V0.8 G1 item 0 (D168 N2): one ``blob_port_is_tls`` value decides the REST +
blob port's TLS, its URL scheme and the D139 node-certificate lock - the
``insecure-dev`` and ``mtls`` combinations, with and without a fabric CA."""

from __future__ import annotations

import pytest

from meshforge.common.errors import ConfigError
from meshforge.controller.http_tls import (
    BlobPortSecurity,
    NodeCertPolicy,
    blob_port_security,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

FABRIC = "fab_0123456789abcdef"


def _revoked() -> frozenset[str]:
    return frozenset()


@pytest.mark.parametrize("server_tls_ready", [True, False])
def test_insecure_dev_is_plain_http_with_no_cert_lock_ca_or_not(
    server_tls_ready: bool,
) -> None:
    port = blob_port_security(insecure=True, server_tls_ready=server_tls_ready)
    assert port.blob_port_is_tls is False
    assert port.scheme == "http"
    # The V0.7 G7 defect: a CA in the data dir must not lock plain HTTP.
    assert port.node_cert_policy(FABRIC, _revoked) is None
    port.check_url("http://127.0.0.1:47110", setting="advertise_url")
    with pytest.raises(ConfigError) as refused:
        port.check_url("https://127.0.0.1:47110", setting="advertise_url")
    assert refused.value.details["reason"] == "blob_url_scheme"


def test_mtls_is_tls_with_the_cert_lock() -> None:
    port = blob_port_security(insecure=False, server_tls_ready=True)
    assert port.blob_port_is_tls is True
    assert port.scheme == "https"
    policy = port.node_cert_policy(FABRIC, _revoked)
    assert isinstance(policy, NodeCertPolicy) and policy.fabric_id == FABRIC
    port.check_url("https://ctl.lan:47110", setting="advertise_url")
    port.check_url(None, setting="advertise_url")
    with pytest.raises(ConfigError):
        port.check_url("http://ctl.lan:47110", setting="advertise_url")


def test_mtls_without_a_server_certificate_fails_closed() -> None:
    with pytest.raises(ConfigError) as refused:
        blob_port_security(insecure=False, server_tls_ready=False)
    assert refused.value.details["reason"] == "no_server_cert"


def test_no_fabric_means_no_lock_even_on_tls() -> None:
    assert (
        BlobPortSecurity(blob_port_is_tls=True).node_cert_policy(None, _revoked) is None
    )
