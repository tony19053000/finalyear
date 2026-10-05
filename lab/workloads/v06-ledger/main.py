# ruff: noqa: T201, S108 - a lab workload: prints its result; the Linux ledger lives in /tmp
"""V0.6 lab workload (roadmap 0.6.13): leaves evidence of every execution.

Usage (the job's entrypoint): ``python main.py <sleep_s> <work_s> <exit_code>``

1. appends ``start`` to this laptop's ledger file;
2. busy-computes for ``work_s`` seconds (CPU load), then sleeps ``sleep_s``;
3. writes ``out/result.txt`` (the job's output: job, attempt, host);
4. appends ``end`` and exits with ``exit_code``.

Ledger: ``C:\\mf-lab\\v06-ledger\\<host>.txt`` on Windows,
``/tmp/mf-lab/v06-ledger/<host>.txt`` elsewhere; one line per event,
``<unix time> <kind> <job_id> <attempt_id> <host>``, each one O_APPEND write.
A run killed with its agent never writes ``end``. ``MF_LAB_LEDGER`` overrides the
folder (tests; the agent does not pass it to jobs). ``lab/sched_check.py check``
reads every laptop's file. Needs an unsandboxed runtime (venv or subprocess):
the Docker sandbox cannot write outside the job folder.
"""

import os
import socket
import sys
import time

sleep_s, work_s, exit_code = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
job = os.environ.get("MESHFORGE_JOB_ID", "unknown")
attempt = os.environ.get("MESHFORGE_ATTEMPT_ID", "unknown")
host = socket.gethostname().replace(" ", "_") or "host"
root = os.environ.get("MF_LAB_LEDGER") or (
    r"C:\mf-lab\v06-ledger" if os.name == "nt" else "/tmp/mf-lab/v06-ledger"
)
os.makedirs(root, exist_ok=True)


def record(kind: str) -> None:
    line = f"{time.time():.6f} {kind} {job} {attempt} {host}\n"
    fd = os.open(
        os.path.join(root, host + ".txt"),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        0o644,
    )
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


record("start")
deadline = time.monotonic() + work_s
x = 0
while time.monotonic() < deadline:
    x = (x * 1103515245 + 12345) % 2147483648
time.sleep(sleep_s)
os.makedirs("out", exist_ok=True)
with open(os.path.join("out", "result.txt"), "w") as fh:
    fh.write(f"{job} {attempt} {host}\n")
record("end")
print("done", job, attempt, host, flush=True)
sys.exit(exit_code)
