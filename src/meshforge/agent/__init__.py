"""Node agent: the long-running service on every laptop in the fabric.

Will own node identity, hardware discovery, resource monitoring, the outbound
gRPC session to the controller, job execution in sandboxes, the blob cache and
peer blob server, network probes and GPU-service supervision (ARCHITECTURE §3).
Allowed imports: ``meshforge.common`` plus the ``agent`` extra (psutil, NVML,
...). It must never import ``meshforge.controller``, nor ``sdk`` or ``cli``; it
supervises ``gpu_service`` as a separate process rather than importing it.
Heavy extra dependencies are imported lazily so that the base install can run
``meshforge-agent --version``.
"""
