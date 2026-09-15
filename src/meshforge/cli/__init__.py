"""The ``compute`` command-line tool for users and operators.

Will cover submit, inspect, logs, cancel, explain, fabric and node management and
benchmarks (ARCHITECTURE §20). Allowed imports: ``meshforge.common`` and
``meshforge.sdk``. It reaches the controller only through its REST API, and must
never import ``controller``, ``agent`` or ``gpu_service`` internals, so it works
on a base install on any laptop.
"""
