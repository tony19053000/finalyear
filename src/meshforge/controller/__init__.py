"""Control plane: the single controller process of a MeshForge fabric.

Will host the REST/WebSocket API, the agent gRPC session service, node registry,
job manager, planner, dispatcher, blob index, telemetry, benchmarks, ML and
enrollment (ARCHITECTURE §3). Allowed imports: ``meshforge.common`` plus the
``controller`` extra (FastAPI, SQLAlchemy, ...). It must never import
``meshforge.agent`` (controller and agent talk only over gRPC), nor
``gpu_service``, ``sdk`` or ``cli``. Heavy extra dependencies are imported
lazily inside functions so that the base install can run
``meshforge-controller --version``.
"""
