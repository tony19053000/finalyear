"""Spike S7 (roadmap 0.2.11): per-laptop hardware scores.

Measurement code as plain functions, no CLI (the CLI is
``lab/spikes/s7_hardware.py``). The numbers produced here are the hardware
inputs of the planner cost model (ARCHITECTURE §7): ``T_compute`` and ``rate_g``
are first estimated from a node's CPU GFLOPS / memory bandwidth / GPU TFLOPS and
later learned from history, so every score must be **comparable across
machines** (same formula, same problem shape) and **reproducible**
(>= 5 repetitions, mean +- stdev, recorded conditions).

Modules:

* :mod:`.scores` - the score arithmetic (FLOPs, bytes moved, throttling).
* :mod:`.sizing` - problem-size selection from VRAM / RAM / cache and calibration.
* :mod:`.environment` - governor, power source and thermal state before/after.
* :mod:`.child` - the benchmark child process (torch / numpy / stdlib).
* :mod:`.worker` - parent side of the child protocol: bounded, never hangs.
* :mod:`.run` - orchestration: cases -> result envelope.
* :mod:`.report` - one table per metric over all hosts (INVALID / TBD).
"""

from typing import Final

#: Spike id and result topic (used in the envelope and the raw folder name).
SPIKE: Final = "S7"
TOPIC: Final = "S7-hardware"
