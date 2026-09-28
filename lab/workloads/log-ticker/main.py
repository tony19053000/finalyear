# ruff: noqa: T201 - a lab workload: its output is the point
"""V0.4 lab (0.4.15, criterion 2): print a timestamped line every 100 ms.

Each line is ``tick <i> <wall time>``; ``lab/exec_check.py latency`` compares
the time it *receives* a line on the controller laptop with the time printed
here. The wall clocks of the two laptops must be synchronised (checklist step
A4) - the report records the measured offset next to the result.
"""

import sys
import time

time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 3.0)  # follower connects first
for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 100):
    print(f"tick {i} {time.time():.6f}", flush=True)
    time.sleep(0.1)
