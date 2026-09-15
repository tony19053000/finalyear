"""GPU service: the per-GPU-node process that executes Mode 2 and Mode 4 work.

Spawned and supervised by the agent in its own environment with PyTorch + CUDA;
will serve sessions, tensors, modules, calls and collectives over the
``GpuService`` gRPC API (ARCHITECTURE §3, §10, §11). Allowed imports:
``meshforge.common`` plus the ``gpu`` extra (PyTorch). It must never import
``controller``, ``agent``, ``sdk`` or ``cli``, so it stays a small, isolated,
independently restartable process.
"""
