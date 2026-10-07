"""Pairing on the controller (V0.7 G2, D135/D136/D150/D151).

* :mod:`.invites` - join tokens in PostgreSQL (create / list / revoke / redeem)
  and the ``audit_event`` trail;
* :mod:`.codes` - turning an invite into a join code (the address and CA pin);
* :mod:`.service` - one ``Enroll``: verify the CSR, redeem the token, check the
  node's key, issue the certificate, record it - one transaction;
* :mod:`.listener` - the interim TLS gRPC listener that serves ``Enroll`` (G2
  only; G3 folds it into the session port) and its per-IP rate limit.
"""
