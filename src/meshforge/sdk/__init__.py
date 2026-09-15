"""User-facing Python SDK imported inside user programs.

Will provide ``remote_gpu()`` (Mode 2), ``combined_gpu()`` (Mode 4),
``RemoteModule``/``RemoteTensor``, the work-queue client (Mode 3) and planner
placement requests (ARCHITECTURE §20). Allowed imports: ``meshforge.common``
only, so it stays installable without the ``controller``/``agent`` extras; it
talks to the fabric over the network, never by importing ``controller``,
``agent``, ``gpu_service`` or ``cli``.
"""
