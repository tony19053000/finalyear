"""Shared foundation used by every other MeshForge subpackage.

Holds configuration, ID generation, structured logging, hashing helpers, error
base classes, process exit codes and (later) generated protobuf code. ``common``
is the bottom layer: it may import only the standard library and core
dependencies, and must never import ``controller``, ``agent``, ``gpu_service``,
``sdk`` or ``cli``. Every other subpackage may import ``common``.
"""
