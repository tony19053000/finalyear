# ruff: noqa: ASYNC240, F811  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2: real controller + agent + PostgreSQL.

* a secret named ``LD_AUDIT`` is still delivered as a FILE; as an env var (any
  case: ``ld_preload``, ``Ld_Audit``) it is refused at submit (T-9);
* two secrets whose values overlap in the job's output: neither value (nor the
  tail of the second) survives in the stored log or the REST log (T-2), the
  job's own file/env delivery still works, and the secrets folder is gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from mf_exec_harness import submit, wait_job

# fixtures reused from the r1 module
from test_v07_g6_tester_r1_jobs import (  # noqa: F401
    _agent,
    alice,
    alice_token,
    settings,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

A = b"abcd12-OVERLAP"
B = b"12-OVERLAP-wxyz"
JOB = r"""
import os, sys
d = os.environ["MESHFORGE_SECRETS_DIR"]
print("SECRETS_DIR=" + d, flush=True)
a = open(os.path.join(d, "OVERA"), "rb").read()
b = open(os.path.join(d, "OVERB"), "rb").read()
ld = open(os.path.join(d, "LD_AUDIT"), "rb").read()
assert a == b"abcd12-OVERLAP" and b == b"12-OVERLAP-wxyz" and ld == b"ld-file-secret-0001"
print("start|" + "abcd12-OVERLAP-wxyz" + "|end", flush=True)   # union
sys.stderr.write("err|abcd12-OVERLAP|and|12-OVERLAP-wxyz|\n")
print("LD_FILE_OK", flush=True)
"""


async def test_ld_named_file_secret_works_overlapping_values_leave_no_tail(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    for name, value in (
        ("OVERA", A),
        ("OVERB", B),
        ("LD_AUDIT", b"ld-file-secret-0001"),
    ):
        assert (await alice.put(f"/v1/secrets/{name}", content=value)).is_success
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(JOB)
    agent = await _agent(grpc_target, tmp_path / "agent")
    try:
        # as an env var: refused at submit, any spelling
        for bad in ("LD_AUDIT", "ld_audit", "Ld_Audit"):
            with pytest.raises(AssertionError) as exc:
                await submit(
                    alice,
                    project,
                    None,
                    {
                        "name": "r2-ld",
                        "entrypoint": ["python", "main.py"],
                        "secrets": [{"name": bad, "as_env": True}],
                    },
                )
            assert "400" in str(exc.value) or "invalid" in str(exc.value).lower()
        job_id = await submit(
            alice,
            project,
            None,
            {
                "name": "r2-over",
                "entrypoint": ["python", "main.py"],
                "secrets": ["OVERA", "OVERB", "LD_AUDIT"],
                "retry": {"max_attempts": 1},
            },
        )
        job = await wait_job(alice, job_id, timeout_s=90)
        assert job["state"] == "succeeded", job
        out = (
            await alice.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
        ).content
        err = (
            await alice.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stderr"})
        ).content
        assert b"LD_FILE_OK" in out
        for blob in (out, err):
            for fragment in (
                b"OVERLAP",
                b"wxyz",
                b"abcd12",
                b"12-OVER",
                b"ld-file-secret",
            ):
                assert fragment not in blob, (fragment, blob)
        assert b"start|[secret:OVERA]|end" in out, out
        assert b"[secret:OVERB]" not in out  # the merged interval names only the first
        # the secrets folder of the attempt is gone
        folder = next(
            line.split("=", 1)[1]
            for line in out.decode().splitlines()
            if line.startswith("SECRETS_DIR=")
        )
        assert not Path(folder).exists(), folder
        data = tmp_path / "agent"
        for path in data.rglob("*"):
            if path.is_file() and path.stat().st_size < 10_000_000:
                blob = path.read_bytes()
                assert (
                    A not in blob
                    and B not in blob
                    and b"ld-file-secret-0001" not in blob
                ), path
    finally:
        await agent.stop()
