"""Spike S3 (roadmap 0.2.7): transport throughput - raw TCP vs gRPC vs HTTP.

Modules:

* :mod:`.protocol` - sizes, chunk sizes, control messages, address safety, errors.
* :mod:`.payload`  - the deterministic byte stream and its CRC-32 verification
  (nothing is ever held in RAM: bytes are generated and discarded as they fly).
* :mod:`.meters`   - CPU time, peak RSS, and the server-side transfer log.
* :mod:`.tcp_server` / :mod:`.http_server` / :mod:`.grpc_server` - one sender per
  transport; :mod:`.server` runs all three in one process.
* :mod:`.client`   - one repetition of one transfer, per transport.
* :mod:`.run`      - the case matrix -> a result envelope.
* :mod:`.tables`   - the S3 tables and the Q2 verdict in the generated report.
"""
