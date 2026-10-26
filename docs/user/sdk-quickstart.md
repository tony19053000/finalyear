# Use MeshForge from your own Python program

Your program can send jobs to the fabric and use their results, the same way you do in a terminal: through the
`compute` command, whose `--json` output is meant for programs. Every `compute` command works this way, and
exit codes are stable (0 = success; see `compute --help`).

**What is not here yet:** the Python library for using another laptop's GPU from inside your running program
(`remote_gpu()`, Remote GPU / Combined GPU) comes with a later version (roadmap V0.11). This page shows what
works today.

## Before you start

- `compute` is installed (it comes with the agent package, or `pip install` of a MeshForge checkout) and you are
  logged in once: `compute login --controller https://<controller>:47110 --fingerprint <CA fingerprint> -u <you>`.
- A laptop in the fabric accepts jobs ([owner-settings.md](owner-settings.md)).

## Example: send a job, wait, read its result

Copy this into `sum_on_fabric.py` and run `python sum_on_fabric.py`. It makes a tiny project, sends it, waits
for it to finish, downloads the output and prints `total = 5050`.

> This exact code is run by MeshForge's test suite against a real fabric
> (`tests/cluster/test_v017_g5_user_docs.py`), so it stays correct.

<!-- ci:run-python -->
```python
"""Send a job to MeshForge from Python and use its result."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def compute(*args: str) -> str:
    """Run one `compute` command; stop with its message if it fails."""
    done = subprocess.run(["compute", *args], capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"compute {args[0]} failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout


# 1. a project: the program and its compute.yaml
project = Path(tempfile.mkdtemp()) / "sum"
project.mkdir()
(project / "compute.yaml").write_text(
    "name: sum\nentrypoint: [python, job.py]\noutputs: [result.json]\n"
)
(project / "job.py").write_text(
    "import json\n"
    "json.dump({'total': sum(range(1, 101))}, open('result.json', 'w'))\n"
)

# 2. send it; -f waits until the job has ended (the first line is its id)
job_id = compute("run", str(project), "-f").splitlines()[0].strip()

# 3. what happened, as JSON
job = json.loads(compute("jobs", job_id, "--json"))
print("job", job_id, "is", job["state"])

# 4. its output files
compute("get", job_id, "--dest", str(project / "out"))
result = json.loads((project / "out" / "result.json").read_text())
print("total =", result["total"])
```

## Useful commands for programs

| Command | Gives |
|---|---|
| `compute run <folder>` | the job id on the first line (add `-f` to wait for the end; the exit code is then the job's outcome) |
| `compute jobs <job id> --json` | the job: `state`, where it ran, its attempts and outputs |
| `compute jobs --json` | your recent jobs |
| `compute get <job id> --dest <folder>` | the job's output files, checked and unpacked |
| `compute logs <job id>` | its log (stdout and stderr) |
| `compute cancel <job id>` | stops it |
| `compute nodes --json` | the laptops and what they offer |

The controller's REST API (`https://<controller>:47110/v1/...`, OpenAPI at `/v1/openapi.json`) is what
`compute` itself uses; scripting it directly works too, with a token from `compute login`.
