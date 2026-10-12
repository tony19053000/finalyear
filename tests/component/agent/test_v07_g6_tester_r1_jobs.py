"""V0.7 G6 tester r1: secrets through real jobs (brief 5f deliverables 3-5).

Real mTLS controller, real PostgreSQL, a real agent, and ``compute`` as a
separate process. A hostile workload prints its secret in every form on both
streams, ends on the secret with no newline, crashes with it in the exception
text. Afterwards no surface may hold a value: job/attempt JSON, decisions,
logs (REST + the live ``compute run -f`` stream + ``compute logs``), every
database table, structlog output of controller and agent, and every file under
the agent's data dir. A retry must get a *changed* value and a *deleted* secret
must fail the retry typed and never hang.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import sys
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_fabric import ca_file
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

PASSWORD = "alice-password-1"
FILE_SECRET = b"r1-FILE-secret-0123456789-abcXYZ"
ENV_SECRET = b"r1-ENV-secret-p@ss/w0rd+=&"
BIN_SECRET = bytes(range(1, 40)) + b"\xfe\xff\x80"


def forms(value: bytes) -> list[bytes]:
    quoted = urllib.parse.quote(value, safe="")
    return [
        value,
        base64.b64encode(value),
        base64.b64encode(value).rstrip(b"="),
        base64.urlsafe_b64encode(value),
        base64.urlsafe_b64encode(value).rstrip(b"="),
        value.hex().encode(),
        value.hex().upper().encode(),
        quoted.encode(),
        urllib.parse.quote_plus(value, safe="").encode(),
    ]


ALL_FORMS = forms(FILE_SECRET) + forms(ENV_SECRET) + forms(BIN_SECRET)


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def alice_token(pg_settings: DatabaseSettings, runtime: ControllerRuntime) -> str:
    engine: AsyncEngine = create_engine(pg_settings)
    try:
        await create_user(engine, "alice", PASSWORD)
        await create_user(engine, "bob", "bob-password-1")
    finally:
        await engine.dispose()
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        return await login(anon, "alice", PASSWORD)


@pytest_asyncio.fixture
async def alice(
    runtime: ControllerRuntime, alice_token: str
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers=bearer(alice_token),
        timeout=30,
    ) as client:
        yield client


async def _agent(grpc_target: str, data_dir: Path, **extra: Any) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "policy": {"accept_jobs": True, "allow_unsandboxed": True},
            **extra,
        },
    )


async def _compute(
    runtime: ControllerRuntime, token: str, tmp_path: Path, *args: str
) -> tuple[int, str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__CA_FILE": str(ca_file()),
            "MESHFORGE_CLI__TOKEN": token,
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 120)
    return (
        int(proc.returncode or 0),
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


LEAKY = r"""
import base64, os, sys, time, urllib.parse
d = os.environ["MESHFORGE_SECRETS_DIR"]
fv = open(os.path.join(d, "FILE_KEY"), "rb").read()
bv = open(os.path.join(d, "BIN_KEY"), "rb").read()
ev = os.environ["ENV_KEY"].encode()
def forms(v):
    return [v, base64.b64encode(v), base64.b64encode(v).rstrip(b"="),
            base64.urlsafe_b64encode(v), base64.urlsafe_b64encode(v).rstrip(b"="),
            v.hex().encode(), v.hex().upper().encode(),
            urllib.parse.quote(v, safe="").encode(),
            urllib.parse.quote_plus(v, safe="").encode()]
mode = sys.argv[1]
for out in (sys.stdout.buffer, sys.stderr.buffer):
    for v in (fv, ev, bv):
        for f in forms(v):
            out.write(b"L<" + f + b">\n")
    # one byte per write, split over many chunks
    for i in range(len(fv)):
        out.write(fv[i:i+1]); out.flush()
        if i % 8 == 0: time.sleep(0.01)
    out.write(b"\n")
    # env var echoed the way a careless app does
    out.write(("config: " + repr(dict(KEY=fv.decode())) + "\n").encode())
    out.flush()
if mode == "crash":
    raise RuntimeError("cannot log in with " + fv.decode())
# the very last bytes of stdout are the secret, no newline
sys.stdout.buffer.write(b"final:" + fv); sys.stdout.buffer.flush()
sys.stderr.buffer.write(b"final:" + ev); sys.stderr.buffer.flush()
"""


def _leaky_project(tmp_path: Path) -> Path:
    project = tmp_path / "leaky"
    project.mkdir()
    (project / "main.py").write_text(LEAKY)
    return project


async def _store_all(alice: httpx.AsyncClient) -> None:
    for name, value in (
        ("FILE_KEY", FILE_SECRET),
        ("ENV_KEY", ENV_SECRET),
        ("BIN_KEY", BIN_SECRET),
    ):
        assert (await alice.put(f"/v1/secrets/{name}", content=value)).is_success


MANIFEST = {
    "name": "r1-leaky",
    "secrets": ["FILE_KEY", {"name": "ENV_KEY", "as_env": True}, "BIN_KEY"],
    "retry": {"max_attempts": 1},
}


def _scan(blob: bytes, where: str) -> list[str]:
    return [f"{where}: {f[:20]!r}" for f in ALL_FORMS if f in blob]


async def test_no_surface_holds_a_value_after_hostile_jobs(
    alice: httpx.AsyncClient,
    alice_token: str,
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    await _store_all(alice)
    agent_dir = tmp_path / "agent"
    agent = await _agent(grpc_target, agent_dir)
    leaks: list[str] = []
    try:
        project = _leaky_project(tmp_path)
        ok_id = await submit(
            alice,
            project,
            None,
            {**MANIFEST, "entrypoint": ["python", "main.py", "ok"]},
        )
        ok = await wait_job(alice, ok_id, timeout_s=90)
        assert ok["state"] == "succeeded", ok
        crash_id = await submit(
            alice,
            project,
            None,
            {**MANIFEST, "entrypoint": ["python", "main.py", "crash"]},
        )
        crash = await wait_job(alice, crash_id, timeout_s=90)
        assert crash["state"] == "failed", crash
        assert crash["failure_type"] == "user_error", crash

        # REST surfaces
        for job_id in (ok_id, crash_id):
            for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/decisions"):
                leaks += _scan((await alice.get(path)).content, path)
            for stream in ("stdout", "stderr"):
                r = await alice.get(
                    f"/v1/jobs/{job_id}/logs", params={"stream": stream}
                )
                leaks += _scan(r.content, f"logs {stream}")
                assert b"[secret:FILE_KEY]" in r.content, (stream, r.content[:400])
        leaks += _scan((await alice.get("/v1/jobs")).content, "job list")
        leaks += _scan((await alice.get("/v1/nodes")).content, "nodes")
        stderr = (
            await alice.get(f"/v1/jobs/{ok_id}/logs", params={"stream": "stderr"})
        ).content
        assert stderr.endswith(b"final:[secret:ENV_KEY]"), stderr[-120:]
        stdout = (
            await alice.get(f"/v1/jobs/{ok_id}/logs", params={"stream": "stdout"})
        ).content
        assert stdout.endswith(b"final:[secret:FILE_KEY]"), stdout[-120:]
        crash_err = (
            await alice.get(f"/v1/jobs/{crash_id}/logs", params={"stream": "stderr"})
        ).content
        assert b"RuntimeError: cannot log in with [secret:FILE_KEY]" in crash_err

        # CLI: the live stream and the stored logs, as a real process
        job_dir = tmp_path / "cli-job"
        job_dir.mkdir()
        (job_dir / "main.py").write_text(LEAKY)
        (job_dir / "compute.yaml").write_text(
            "name: r1-cli\n"
            "entrypoint: [python, main.py, ok]\n"
            "secrets: [FILE_KEY, {name: ENV_KEY, as_env: true}, BIN_KEY]\n"
            "retry: {max_attempts: 1}\n"
        )
        code, out, err = await _compute(
            runtime, alice_token, tmp_path, "run", str(job_dir), "-f"
        )
        assert code == 0, (code, out[-400:], err[-400:])
        leaks += _scan(out.encode("utf-8", "surrogateescape"), "cli run -f stdout")
        leaks += _scan(err.encode("utf-8", "surrogateescape"), "cli run -f stderr")
        assert "[secret:FILE_KEY]" in out + err
        cli_job = out.splitlines()[0].strip()
        for args in (
            ("logs", cli_job),
            ("explain", cli_job),
            ("jobs",),
            ("get", cli_job),
            ("secret", "list"),
            ("nodes",),
        ):
            code, out2, err2 = await _compute(runtime, alice_token, tmp_path, *args)
            leaks += _scan(
                (out2 + err2).encode("utf-8", "surrogateescape"), f"cli {args}"
            )

        # every database table
        engine = create_engine(pg_settings)
        try:
            async with engine.connect() as conn:
                tables = [
                    r[0]
                    for r in await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                        )
                    )
                ]
                for table in tables:
                    if table == "secret":
                        continue
                    rows = await conn.execute(text(f'SELECT t::text FROM "{table}" t'))  # noqa: S608
                    leaks += _scan(
                        "\n".join(r[0] for r in rows).encode(), f"db {table}"
                    )
        finally:
            await engine.dispose()

        # our own structlog/stdlib output (controller + agent share this process)
        leaks += _scan(caplog.text.encode("utf-8", "replace"), "log records")
        for record in caplog.records:
            leaks += _scan(repr(record.__dict__).encode(), "log record dict")

        # every file the agent wrote
        for path in agent_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    leaks += _scan(path.read_bytes(), f"agent file {path.name}")
        vault = agent.executor.vault
        assert list(vault.base.iterdir()) == []
    finally:
        await agent.stop()
    assert leaks == [], leaks


SEEN = r"""
import hashlib, os, sys, time
d = os.environ["MESHFORGE_SECRETS_DIR"]
n = len([f for f in os.listdir(sys.argv[1]) if f.startswith("seen-")]) + 1
v = open(os.path.join(d, "ROT"), "rb").read()
open(os.path.join(sys.argv[1], f"seen-{n}"), "w").write(hashlib.sha256(v).hexdigest())
time.sleep(60)
"""


def _wait_sync(path: Path) -> bool:
    return path.exists()


async def _wait_for(path: Path, timeout_s: float = 60) -> None:
    for _ in range(int(timeout_s / 0.05)):
        if await asyncio.to_thread(_wait_sync, path):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


async def _running_job(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    value: bytes,
    max_attempts: int,
) -> tuple[ExecAgent, ExecAgent, str, Path]:
    assert (await alice.put("/v1/secrets/ROT", content=value)).is_success
    one = await _agent(grpc_target, tmp_path / "agent1")
    two = await _agent(grpc_target, tmp_path / "agent2")
    seen = tmp_path / "seen"
    seen.mkdir()
    project = tmp_path / "rot"
    project.mkdir()
    (project / "main.py").write_text(SEEN)
    job_id = await submit(
        alice,
        project,
        None,
        {
            "name": "r1-rot",
            "entrypoint": ["python", "main.py", str(seen)],
            "secrets": ["ROT"],
            "retry": {"max_attempts": max_attempts, "backoff_s": 0},
        },
    )
    await _wait_for(seen / "seen-1")
    return one, two, job_id, seen


async def _stop_the_node_running(
    alice: httpx.AsyncClient, job_id: str, one: ExecAgent, two: ExecAgent
) -> ExecAgent:
    job = (await alice.get(f"/v1/jobs/{job_id}")).json()
    node = job["attempts"][-1]["node_id"]
    running, other = (one, two) if one.node_id == node else (two, one)
    await running.stop()
    return other


async def test_a_retry_on_another_node_gets_the_changed_value(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    v1, v2 = b"rotate-value-ONE-1234", b"rotate-value-TWO-5678"
    one, two, job_id, seen = await _running_job(alice, grpc_target, tmp_path, v1, 2)
    other = None
    try:
        assert (await alice.put("/v1/secrets/ROT", content=v2)).is_success
        other = await _stop_the_node_running(alice, job_id, one, two)
        await _wait_for(seen / "seen-2", 90)
        assert (await alice.post(f"/v1/jobs/{job_id}/cancel")).is_success
        job = await wait_job(alice, job_id, timeout_s=60)
    finally:
        for agent in (one, two):
            await agent.stop()
    assert (seen / "seen-1").read_text() == hashlib.sha256(v1).hexdigest()
    assert (seen / "seen-2").read_text() == hashlib.sha256(v2).hexdigest()
    assert other is not None and job["attempts"][-1]["node_id"] == other.node_id
    assert len(job["attempts"]) == 2


async def test_a_secret_deleted_before_the_retry_fails_it_typed_and_never_hangs(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    one, two, job_id, seen = await _running_job(
        alice, grpc_target, tmp_path, b"doomed-value-1234", 3
    )
    try:
        assert (await alice.delete("/v1/secrets/ROT")).is_success
        await _stop_the_node_running(alice, job_id, one, two)
        job = await wait_job(alice, job_id, timeout_s=90)
    finally:
        for agent in (one, two):
            await agent.stop()
    assert not (seen / "seen-2").exists(), "the retry started without its secret"
    assert job["state"] == "failed", job
    assert job["failure_type"] == "invalid_input", job
    assert "secret_missing:ROT" in (job["failure_detail"] or ""), job
    last = job["attempts"][-1]
    assert last["state"] == "failed" and "secret_missing:ROT" in last["detail"]
    assert len(job["attempts"]) == 2  # not retried a third time


INSECURE_WORKLOAD = (
    "import os, sys\n"
    "open(sys.argv[1], 'w').write(os.environ.get('MESHFORGE_SECRETS_DIR', 'none'))\n"
)


@pytest.mark.parametrize(
    ("controller_flag", "agent_flag"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_insecure_dev_refuses_secrets_unless_the_explicit_flag_on_both_sides(
    pg_settings: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controller_flag: bool,
    agent_flag: bool,
) -> None:
    import mf_exec_harness
    from mf_agent_harness import AGENT_HELLO_TIMEOUT_S, AGENT_RECONNECT
    from mf_controller_harness import controller_section

    from meshforge.agent.settings import AgentSection, AgentSettings

    security: dict[str, Any] = {
        "mode": "insecure-dev",
        "allow_insecure_secrets": controller_flag,
    }
    agent_security: dict[str, Any] = {
        "mode": "insecure-dev",
        "allow_insecure_secrets": agent_flag,
    }
    settings = ControllerSettings(
        security=security,
        controller=controller_section(
            auth=USERS_ONLY, jobs={"dispatch_interval_s": 0.1}, **tolerant_liveness()
        ),
        database=pg_settings,
    )

    def insecure_agent_settings(
        data_dir: Path, controller_address: str, *, enroll: bool = True, **over: Any
    ) -> AgentSettings:
        agent: dict[str, Any] = {
            "controller_address": controller_address,
            "reconnect": dict(AGENT_RECONNECT),
            "hello_timeout_s": AGENT_HELLO_TIMEOUT_S,
            "identity_lock_timeout_s": 0.5,
            **over,
        }
        return AgentSettings(
            data_dir=data_dir, agent=AgentSection(**agent), security=agent_security
        )

    monkeypatch.setattr(mf_exec_harness, "make_agent_settings", insecure_agent_settings)
    async with ControllerRuntime.start(settings) as runtime:
        engine = create_engine(pg_settings)
        try:
            await create_user(engine, "alice", PASSWORD)
        finally:
            await engine.dispose()
        base = f"http://127.0.0.1:{runtime.http_port}"
        async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
            token = await login(anon, "alice", PASSWORD)
        agent = await _agent(f"127.0.0.1:{runtime.grpc_port}", tmp_path / "agent")
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=bearer(token), timeout=30
            ) as client:
                assert (
                    await client.put("/v1/secrets/K1", content=b"insecure-value-1")
                ).is_success
                project = tmp_path / "proj"
                project.mkdir()
                (project / "main.py").write_text(INSECURE_WORKLOAD)
                ran = tmp_path / "ran.txt"
                job_id = await submit(
                    client,
                    project,
                    None,
                    {
                        "name": "r1-insecure",
                        "entrypoint": ["python", "main.py", str(ran)],
                        "secrets": ["K1"],
                        "retry": {"max_attempts": 1},
                    },
                )
                job = await wait_job(client, job_id, timeout_s=60)
        finally:
            await agent.stop()
    if controller_flag and agent_flag:
        assert job["state"] == "succeeded", job
        assert "meshforge-secrets" in ran.read_text()
        return
    assert job["state"] == "failed", job
    assert job["failure_type"] == "config_error", job
    assert "secrets_need_mtls" in (job["failure_detail"] or ""), job
    assert not ran.exists(), "the workload ran although the channel is plaintext"


@pytest.mark.xfail(
    strict=True,
    reason="T-3 (minor-major): with any secret on the job, the last "
    "max-form-length - 1 bytes of each stream are held back until more output "
    "arrives or the job ends, so a job that prints a short status line and "
    "then works for an hour shows nothing live (`compute run -f`, logs API)",
)
async def test_a_short_status_line_is_visible_live_while_the_job_runs(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    assert (
        await alice.put("/v1/secrets/ROT", content=b"live-view-secret-0123456789")
    ).is_success
    agent = await _agent(grpc_target, tmp_path / "agent")
    project = tmp_path / "live"
    project.mkdir()
    (project / "main.py").write_text(
        "import time\nprint('training started, epoch 1', flush=True)\ntime.sleep(25)\n"
    )
    try:
        job_id = await submit(
            alice,
            project,
            None,
            {
                "name": "r1-live",
                "entrypoint": ["python", "main.py"],
                "secrets": ["ROT"],
                "retry": {"max_attempts": 1},
            },
        )
        seen = b""
        for _ in range(100):  # 10 s
            r = await alice.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
            seen = r.content
            if b"epoch 1" in seen:
                break
            await asyncio.sleep(0.1)
        state = (await alice.get(f"/v1/jobs/{job_id}")).json()["state"]
        assert state == "running"
        assert b"training started, epoch 1" in seen, (
            f"nothing visible 10 s into a running job: {seen!r}"
        )
        await alice.post(f"/v1/jobs/{job_id}/cancel")
    finally:
        await agent.stop()


async def test_naming_someone_elses_secret_is_indistinguishable_from_a_missing_one(
    alice: httpx.AsyncClient,
    alice_token: str,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    """No existence oracle: bob learns nothing about alice's secret names."""
    assert (
        await alice.put("/v1/secrets/ONLY_ALICE", content=b"alice-value-1")
    ).is_success
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        bob_token = await login(anon, "bob", "bob-password-1")
    agent = await _agent(grpc_target, tmp_path / "agent")
    try:
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers=bearer(bob_token),
            timeout=30,
        ) as bob:
            project = tmp_path / "p"
            project.mkdir()
            (project / "main.py").write_text("print(1)\n")
            texts = []
            for name in ("ONLY_ALICE", "NOBODY_HAS_THIS"):
                with pytest.raises(AssertionError) as exc:
                    await submit(
                        bob,
                        project,
                        None,
                        {
                            "name": "probe",
                            "entrypoint": ["python", "main.py"],
                            "secrets": [name],
                        },
                    )
                texts.append(str(exc.value).replace(name, "<N>"))
            assert texts[0] == texts[1], texts
            assert "alice" not in texts[0].lower()
    finally:
        await agent.stop()
