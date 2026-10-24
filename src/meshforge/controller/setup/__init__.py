"""The controller's one-time "create fabric" setup (V0.17 G2, D392, Q20).

A freshly installed controller (``mtls``, no fabric CA, no users) with a setup
token serves only a setup page - plain HTTP on loopback - where the person who
installed it creates the fabric: the first admin user and password, the fabric
CA and server certificate, and the first invite (code + QR). Then the token is
deleted, every setup endpoint answers 410 forever, and the controller restarts
itself into its normal mode.

Modules: :mod:`.token` (the setup token file), :mod:`.policy` (the admin
password rule), :mod:`.state` (when setup applies), :mod:`.service` (the steps),
:mod:`.routes` (the HTTP API + the setup-mode app).
"""
