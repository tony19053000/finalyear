"""Spike S6 (roadmap 0.2.10): execution environments on one laptop.

Answers, per laptop, ARCHITECTURE §27 Q4 and the ADR-0004 premise ("Docker
first, uv venv fallback"): is Docker usable for workloads here, does a
container see the **GPU**, and what does an environment *cost* on this machine
(container start, image cold pull, PyTorch-CUDA container start to first CUDA
op, ``uv`` venv creation with a cold and with a warm cache). Those numbers
calibrate V0.4.7/0.4.8 and V0.5 criterion 5 ("warm env cache: job start
overhead <= 3 s").

Standard library plus the ``docker`` and ``uv`` command lines only: no docker
SDK, no psutil, no torch on the host (torch runs *inside* the container).

Modules:

* :mod:`.proc` - bounded subprocess calls (timeout, no stdin, capped output).
* :mod:`.docker` - ``docker info`` facts, image handling, sandboxed ``docker
  run`` and the typed classification of its failures.
* :mod:`.venv` - ``uv venv`` + ``uv pip install`` with a private cache.
* :mod:`.facts` - host facts (OS, WSL, CPU, RAM, uv, NVIDIA GPU, disk).
* :mod:`.run` - orchestration: cases -> result envelope; cleanup in ``finally``.
* :mod:`.verdict` - verdicts and cost table judged from the JSON only.
* :mod:`.report` - the S6-specific markdown block of the report.
"""

from typing import Final

#: Spike id and result topic (used in the envelope and the raw folder name).
SPIKE: Final = "S6"
TOPIC: Final = "S6-environments"
