"""Orchestration of one S6 run: host facts -> cases -> repetitions -> envelope.

Cases, in order (every case id is fixed; the report and verdict key on them):

======================  ==================================================
``docker-start``        warm start of the small image in the ADR-0004 sandbox
``docker-pull-cold``    ``rmi`` + ``pull`` of the small image (needs network)
``docker-gpu-smi``      ``--gpus all`` + ``nvidia-smi`` in a CUDA base image
``docker-gpu-torch``    PyTorch-CUDA image: import torch, first CUDA matmul
``venv-cold``           ``uv venv`` + install with a fresh empty cache (network)
``venv-warm``           same with a cache warmed once in setup, ``--offline``
``venv-start``          the warm venv's interpreter importing numpy + grpc
======================  ==================================================

What cannot run is recorded, never silently omitted: a case skipped by an
operator flag carries one ``skipped_by_flag`` error record and is listed in
``extra.skipped`` (it does not make the run incomplete - the operator chose
it); a case that cannot start (no Docker, no GPU on the host, not enough
disk, budget gone) carries a typed setup error and makes the exit code 3.
Every temp directory and every container this run created is removed in
``finally`` - also on Ctrl-C.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import make_envelope, sanitize_text
from spikes._harness.reps import CaseResult, RepError, Sample, error_record, run_reps
from spikes._s6 import SPIKE, TOPIC, docker, facts, venv
from spikes._s6.docker import DockerError
from spikes._s6.venv import VenvError

type JSON = Any

DEFAULT_REPS: Final = 5
QUICK_REPS: Final = 2
DEFAULT_WARMUP: Final = 1
DEFAULT_BUDGET_S: Final = 3600.0
QUICK_BUDGET_S: Final = 900.0
#: Per-command caps (each is further clipped by the run budget).
CONTAINER_TIMEOUT_S: Final = 180.0
SMALL_PULL_TIMEOUT_S: Final = 600.0
GPU_TORCH_PULL_TIMEOUT_S: Final = 3600.0
GPU_TORCH_RUN_TIMEOUT_S: Final = 600.0
UV_TIMEOUT_S: Final = 900.0
#: Disk needed for a large pull: compressed layers are downloaded, then
#: extracted (about 2x), and Docker keeps both for a moment.
PULL_DISK_FACTOR: Final = 3
#: When the manifest size cannot be read (no network), assume this much.
DEFAULT_TORCH_COMPRESSED_BYTES: Final = 4_000_000_000

METRIC_UNITS: Final[dict[str, str]] = {
    "start_to_exit_ms": "ms",
    "pull_s": "s",
    "bytes_on_disk": "B",
    "start_to_ready_ms": "ms",
    "total_ms": "ms",
    "import_s": "s",
    "first_cuda_op_s": "s",
    "create_s": "s",
    "install_s": "s",
    "total_s": "s",
    "site_packages_bytes": "B",
    "cache_bytes": "B",
}
SKIPPED: Final = "skipped_by_flag"


def metric_units(names: list[str]) -> dict[str, str]:
    return {name: METRIC_UNITS.get(name, "") for name in names}


class Budget:
    """The whole-run wall-clock budget (``--timeout``)."""

    def __init__(self, total_s: float, clock: Callable[[], float] = time.monotonic):
        self.total_s = total_s
        self._clock = clock
        self._deadline = clock() + total_s

    def remaining(self) -> float:
        return self._deadline - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def slice_for(self, wanted_s: float) -> float:
        return max(0.0, min(wanted_s, self.remaining()))


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything the operator chose for this run."""

    reps: int = 0
    warmup: int = -1
    quick: bool = False
    skip_docker: bool = False
    skip_gpu: bool = False
    skip_gpu_torch: bool = False
    no_network: bool = False
    gpu_torch_image: str = docker.GPU_TORCH_IMAGE
    name: str | None = None
    budget_s: float = 0.0
    pause_s: float = 0.2

    def effective_reps(self) -> int:
        if self.reps:
            return self.reps
        return QUICK_REPS if self.quick else DEFAULT_REPS

    def effective_warmup(self) -> int:
        return DEFAULT_WARMUP if self.warmup < 0 else self.warmup

    def effective_budget(self) -> float:
        if self.budget_s:
            return self.budget_s
        return QUICK_BUDGET_S if self.quick else DEFAULT_BUDGET_S


@dataclass(slots=True)
class EnvironmentRun:
    envelope: dict[str, JSON]
    complete: bool
    warnings: list[str]


@dataclass(slots=True)
class _Context:
    """State shared by the cases of one run."""

    cfg: RunConfig
    budget: Budget
    temp_dir: Path
    log: Callable[[str], None]
    docker: str | None
    docker_facts: dict[str, JSON]
    uv: str | None
    host: dict[str, JSON]
    warm_venv: Path | None = None
    warm_cache: Path | None = None
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    images: dict[str, JSON] = field(default_factory=dict)

    @property
    def gpu_on_host(self) -> bool:
        return bool(self.host.get("gpu", {}).get("present"))

    def docker_ready(self) -> DockerError | None:
        """``None`` when Docker can run containers, else the typed reason."""
        if self.docker is None:
            return DockerError(
                "docker_cli_missing", "docker is not on PATH", fix=docker.FIX_CLI
            )
        if not self.docker_facts.get("ok"):
            return DockerError(
                str(
                    self.docker_facts.get("failure_type") or "docker_daemon_unreachable"
                ),
                str(self.docker_facts.get("message") or "docker is not usable"),
                fix=str(self.docker_facts.get("fix") or ""),
            )
        return None


# ------------------------------------------------------------ case helpers
def _with_fix(exc: DockerError | VenvError) -> str:
    return f"{exc} [fix: {exc.fix}]" if exc.fix else str(exc)


def _case_shell(
    case_id: str, label: str, metrics: dict[str, str], ctx: _Context
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        label=label,
        metrics=metrics,
        requested_reps=ctx.cfg.effective_reps(),
        requested_warmup=ctx.cfg.effective_warmup(),
    )


def _failed_case(
    case_id: str, label: str, metrics: dict[str, str], ctx: _Context, exc: RepError
) -> CaseResult:
    result = _case_shell(case_id, label, metrics, ctx)
    result.aborted = True
    result.errors.append(error_record(0, "setup", exc))
    return result


def _skipped_case(
    case_id: str, label: str, metrics: dict[str, str], ctx: _Context, reason: str
) -> CaseResult:
    ctx.skipped[case_id] = reason
    ctx.log(f"{case_id}: skipped ({reason})")
    return _failed_case(
        case_id, label, metrics, ctx, RepError(SKIPPED, reason, fatal=True)
    )


def _budget_error(ctx: _Context) -> RepError:
    return RepError(
        "budget_exhausted",
        f"the run budget of {ctx.budget.total_s:g} s ran out",
        fatal=True,
    )


def _docker_rep_error(exc: DockerError) -> RepError:
    fatal = exc.failure_type in {
        "docker_daemon_unreachable",
        "docker_permission_denied",
        "no_nvidia_runtime",
        "timeout",
        "sandbox_flag_refused",
    }
    return RepError(exc.failure_type, _with_fix(exc), fatal=fatal)


def _run_case(
    ctx: _Context,
    case_id: str,
    label: str,
    metrics: dict[str, str],
    measure: Callable[[int], Sample],
    attrs: dict[str, JSON],
) -> CaseResult:
    def guarded(index: int) -> Sample:
        if ctx.budget.expired():
            raise _budget_error(ctx)
        return measure(index)

    return run_reps(
        case_id,
        guarded,
        reps=ctx.cfg.effective_reps(),
        warmup=ctx.cfg.effective_warmup(),
        metrics=metrics,
        label=label,
        attrs=attrs,
        pause_s=ctx.cfg.pause_s,
    )


def _ensure_image(ctx: _Context, image: str, *, timeout: float) -> dict[str, JSON]:
    """Pull ``image`` if absent (once); returns its digest/size facts."""
    assert ctx.docker is not None  # noqa: S101 - checked by docker_ready()
    if not docker.image_present(ctx.docker, image):
        if ctx.cfg.no_network:
            raise DockerError(
                "image_pull_failed",
                f"{image} is not present locally and --no-network was given",
                fix="pull it once: docker pull " + image,
            )
        ctx.log(f"pulling {image} (once) ...")
        out = docker.pull_image(
            ctx.docker, image, timeout=ctx.budget.slice_for(timeout)
        )
        if not out.ok:
            raise docker.classify(out, image=image)
    info = docker.image_facts(ctx.docker, image)
    ctx.images[image] = info
    return info


# -------------------------------------------------------------------- cases
def case_docker_start(ctx: _Context) -> CaseResult:
    case_id, label = "docker-start", "warm container start, sandboxed python -c print"
    metrics = metric_units(["start_to_exit_ms"])
    if ctx.cfg.skip_docker:
        return _skipped_case(case_id, label, metrics, ctx, "--skip-docker")
    if (why := ctx.docker_ready()) is not None:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(why))
    try:
        image = _ensure_image(ctx, docker.SMALL_IMAGE, timeout=SMALL_PULL_TIMEOUT_S)
    except DockerError as exc:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(exc))
    cli = ctx.docker
    assert cli is not None  # noqa: S101
    daemon_warnings: list[str] = []

    def measure(_index: int) -> Sample:
        run = docker.run_container(
            cli,
            docker.SMALL_IMAGE,
            ["python", "-c", "print('ok')"],
            timeout=ctx.budget.slice_for(CONTAINER_TIMEOUT_S),
            sandbox=True,
        )
        for warning in run.warnings:
            if warning not in daemon_warnings:
                daemon_warnings.append(warning)
        if not run.outcome.ok:
            raise _docker_rep_error(
                docker.classify(run.outcome, image=docker.SMALL_IMAGE)
            )
        if run.outcome.stdout.strip() != "ok":
            raise RepError("container_failed", "container printed no 'ok'")
        return {"start_to_exit_ms": run.elapsed_ms, "exit_code": run.outcome.returncode}

    attrs: dict[str, JSON] = {
        "image": image,
        "sandbox_flags": list(docker.SANDBOX_FLAGS),
        "command": "python -c print('ok')",
        "daemon_warnings": daemon_warnings,
    }
    return _run_case(ctx, case_id, label, metrics, measure, attrs)


def case_docker_pull_cold(ctx: _Context) -> CaseResult:
    case_id, label = "docker-pull-cold", "cold pull of the small image (rmi, then pull)"
    metrics = metric_units(["pull_s", "bytes_on_disk"])
    if ctx.cfg.skip_docker:
        return _skipped_case(case_id, label, metrics, ctx, "--skip-docker")
    if ctx.cfg.no_network:
        return _skipped_case(case_id, label, metrics, ctx, "--no-network")
    if (why := ctx.docker_ready()) is not None:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(why))
    cli = ctx.docker
    assert cli is not None  # noqa: S101
    image = docker.SMALL_IMAGE

    def measure(_index: int) -> Sample:
        if docker.image_present(cli, image):
            removed = docker.remove_image(cli, image)
            if not removed.ok:
                raise RepError(
                    "image_remove_failed",
                    f"docker rmi {image}: {sanitize_text(removed.describe()[:300])}",
                    fatal=True,
                )
        out = docker.pull_image(
            cli, image, timeout=ctx.budget.slice_for(SMALL_PULL_TIMEOUT_S)
        )
        if not out.ok:
            raise _docker_rep_error(docker.classify(out, image=image))
        info = docker.image_facts(cli, image)
        return {
            "pull_s": out.elapsed_s,
            "bytes_on_disk": info.get("size_bytes"),
            "digest": info.get("digest"),
        }

    try:
        return _run_case(ctx, case_id, label, metrics, measure, {"image": image})
    finally:
        # leave the image behind for later cases / the next run
        if not docker.image_present(cli, image):
            docker.pull_image(
                cli, image, timeout=ctx.budget.slice_for(SMALL_PULL_TIMEOUT_S)
            )


def _gpu_gate(
    ctx: _Context, case_id: str, label: str, metrics: dict[str, str]
) -> CaseResult | None:
    """The typed reason a GPU case cannot run, as a case, or ``None``."""
    if ctx.cfg.skip_docker:
        return _skipped_case(case_id, label, metrics, ctx, "--skip-docker")
    if ctx.cfg.skip_gpu:
        return _skipped_case(case_id, label, metrics, ctx, "--skip-gpu")
    if (why := ctx.docker_ready()) is not None:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(why))
    gpu = ctx.host.get("gpu") or {}
    if not gpu.get("present"):
        return _failed_case(
            case_id,
            label,
            metrics,
            ctx,
            RepError(
                "no_gpu_on_host",
                f"{gpu.get('message', 'no NVIDIA GPU')} [fix: {gpu.get('fix', '')}]",
                fatal=True,
            ),
        )
    return None


def case_docker_gpu_smi(ctx: _Context) -> CaseResult:
    case_id, label = (
        "docker-gpu-smi",
        "nvidia-smi inside a CUDA base container (--gpus all)",
    )
    metrics = metric_units(["start_to_exit_ms"])
    if (gate := _gpu_gate(ctx, case_id, label, metrics)) is not None:
        return gate
    try:
        image = _ensure_image(ctx, docker.GPU_SMI_IMAGE, timeout=SMALL_PULL_TIMEOUT_S)
    except DockerError as exc:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(exc))
    cli = ctx.docker
    assert cli is not None  # noqa: S101
    seen: dict[str, JSON] = {}

    def measure(_index: int) -> Sample:
        run = docker.run_container(
            cli,
            docker.GPU_SMI_IMAGE,
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            timeout=ctx.budget.slice_for(CONTAINER_TIMEOUT_S),
            gpus=True,
        )
        if not run.outcome.ok:
            raise _docker_rep_error(
                docker.classify(run.outcome, image=docker.GPU_SMI_IMAGE)
            )
        row = docker.parse_smi_row(run.outcome.stdout)
        if not row.get("gpu_name"):
            raise RepError("nvidia_driver_error", "nvidia-smi printed no GPU row")
        seen.update(row)
        return {"start_to_exit_ms": run.elapsed_ms, "exit_code": run.outcome.returncode}

    attrs: dict[str, JSON] = {"image": image, "gpu": seen}
    return _run_case(ctx, case_id, label, metrics, measure, attrs)


def _torch_image_ready(ctx: _Context, image: str) -> tuple[dict[str, JSON], JSON]:
    """Pull the PyTorch image once if absent; returns (image facts, cold-pull note)."""
    cli = ctx.docker
    assert cli is not None  # noqa: S101
    if docker.image_present(cli, image):
        return docker.image_facts(cli, image), None
    if ctx.cfg.no_network:
        raise DockerError(
            "image_pull_failed",
            f"{image} is not present locally and --no-network was given",
            fix=f"docker pull {image}",
        )
    compressed = docker.compressed_size(cli, image)
    need = PULL_DISK_FACTOR * (compressed or DEFAULT_TORCH_COMPRESSED_BYTES)
    free_map = ctx.host.get("disk_free_bytes") or {}
    free = free_map.get("docker_root") or free_map.get("temp_dir")
    if isinstance(free, int) and free < need:
        raise DockerError(
            "insufficient_disk",
            f"{free / 1e9:.1f} GB free, about {need / 1e9:.1f} GB needed to pull "
            f"{image} ({(compressed or DEFAULT_TORCH_COMPRESSED_BYTES) / 1e9:.1f} GB compressed)",
            fix="free disk space (e.g. docker system prune / docker builder prune) "
            "or pass --skip-gpu-torch",
        )
    ctx.log(
        f"!! downloading {image} (~{(compressed or DEFAULT_TORCH_COMPRESSED_BYTES) / 1e9:.1f} GB "
        "compressed) once; Ctrl-C now to skip, or pass --skip-gpu-torch"
    )
    out = docker.pull_image(
        cli, image, timeout=ctx.budget.slice_for(GPU_TORCH_PULL_TIMEOUT_S)
    )
    if not out.ok:
        raise docker.classify(out, image=image)
    info = docker.image_facts(cli, image)
    note = {
        "cold_pull_s": out.elapsed_s,
        "compressed_bytes": compressed,
        "size_bytes": info.get("size_bytes"),
        "note": "single informational measurement (n=1), not a sample of any case",
    }
    return info, note


def case_docker_gpu_torch(ctx: _Context) -> CaseResult:
    case_id = "docker-gpu-torch"
    label = "PyTorch-CUDA container: import torch + first CUDA matmul"
    metrics = metric_units(
        ["start_to_ready_ms", "total_ms", "import_s", "first_cuda_op_s"]
    )
    if ctx.cfg.skip_gpu_torch:
        return _skipped_case(case_id, label, metrics, ctx, "--skip-gpu-torch")
    if ctx.cfg.quick:
        return _skipped_case(case_id, label, metrics, ctx, "--quick")
    if (gate := _gpu_gate(ctx, case_id, label, metrics)) is not None:
        return gate
    image_name = ctx.cfg.gpu_torch_image
    try:
        image, cold_pull = _torch_image_ready(ctx, image_name)
    except DockerError as exc:
        return _failed_case(case_id, label, metrics, ctx, _docker_rep_error(exc))
    ctx.images[image_name] = image
    cli = ctx.docker
    assert cli is not None  # noqa: S101
    seen: dict[str, JSON] = {}

    def measure(_index: int) -> Sample:
        run = docker.run_container(
            cli,
            image_name,
            ["python", "-c", docker.TORCH_SCRIPT],
            timeout=ctx.budget.slice_for(GPU_TORCH_RUN_TIMEOUT_S),
            gpus=True,
        )
        line = docker.parse_torch_line(run.outcome.stdout)
        if run.outcome.returncode == 3 and line is not None:
            raise RepError(
                "cuda_unavailable_in_container",
                f"torch {line.get('torch_version')} imported but torch.cuda.is_available() "
                "is False inside the container",
                fatal=True,
            )
        if not run.outcome.ok:
            raise _docker_rep_error(docker.classify(run.outcome, image=image_name))
        if line is None:
            raise RepError("container_failed", "no JSON line from the torch script")
        seen.update(
            {
                k: line.get(k)
                for k in ("torch_version", "torch_cuda", "device_name")
                if isinstance(line.get(k), str)
            }
        )
        sample: Sample = {
            "total_ms": run.elapsed_ms,
            "exit_code": run.outcome.returncode,
            "start_to_ready_ms": None,
            "clock_suspect": False,
        }
        for key in ("import_s", "first_cuda_op_s"):
            value = line.get(key)
            sample[key] = float(value) if isinstance(value, int | float) else None
        ready = line.get("ready_wall_s")
        if isinstance(ready, int | float):
            ready_ms = (float(ready) - run.launch_wall_s) * 1000.0
            if 0.0 <= ready_ms <= run.elapsed_ms:
                sample["start_to_ready_ms"] = ready_ms
            else:
                sample["clock_suspect"] = True
        return sample

    attrs: dict[str, JSON] = {
        "image": image,
        "cold_pull": cold_pull,
        "torch": seen,
        "matmul": "1024x1024 fp32, torch.cuda.synchronize()",
        "start_to_ready_definition": (
            "container ready_wall_s (after import torch + first CUDA op) minus the "
            "host's docker run launch time; None + clock_suspect when the two clocks disagree"
        ),
    }
    return _run_case(ctx, case_id, label, metrics, measure, attrs)


def _uv_gate(
    ctx: _Context, case_id: str, label: str, metrics: dict[str, str]
) -> CaseResult | None:
    if ctx.uv is None:
        return _failed_case(
            case_id,
            label,
            metrics,
            ctx,
            RepError(
                "uv_cli_missing", "uv is not on PATH [fix: install uv]", fatal=True
            ),
        )
    return None


def _venv_rep_error(exc: VenvError) -> RepError:
    return RepError(
        exc.failure_type, _with_fix(exc), fatal=exc.failure_type != "uv_failed"
    )


def _build_venv(
    ctx: _Context, venv_dir: Path, cache_dir: Path, requirements: Path, *, offline: bool
) -> Sample:
    uv = ctx.uv
    assert uv is not None  # noqa: S101
    try:
        create_s = venv.create_venv(
            uv,
            venv_dir,
            cache_dir=cache_dir,
            timeout=ctx.budget.slice_for(UV_TIMEOUT_S),
        )
        install_s = venv.install(
            uv,
            venv_dir,
            requirements,
            cache_dir=cache_dir,
            offline=offline,
            timeout=ctx.budget.slice_for(UV_TIMEOUT_S),
        )
    except VenvError as exc:
        raise _venv_rep_error(exc) from exc
    return {
        "create_s": create_s,
        "install_s": install_s,
        "total_s": create_s + install_s,
        "site_packages_bytes": venv.dir_bytes(venv.site_packages(venv_dir)),
        "cache_bytes": venv.dir_bytes(cache_dir),
    }


_VENV_METRICS: Final = [
    "create_s",
    "install_s",
    "total_s",
    "site_packages_bytes",
    "cache_bytes",
]


def _venv_attrs(ctx: _Context, *, offline: bool) -> dict[str, JSON]:
    return {
        "pinned_requirements": list(venv.PINNED_REQUIREMENTS),
        "python_request": venv.PYTHON_REQUEST,
        "offline": offline,
        "uv_version": ctx.host.get("uv_version"),
    }


def case_venv_cold(ctx: _Context) -> CaseResult:
    case_id, label = "venv-cold", "uv venv + install, fresh empty cache per repetition"
    metrics = metric_units(_VENV_METRICS)
    if ctx.cfg.no_network:
        return _skipped_case(case_id, label, metrics, ctx, "--no-network")
    if (gate := _uv_gate(ctx, case_id, label, metrics)) is not None:
        return gate
    requirements = venv.write_requirements(ctx.temp_dir)

    def measure(index: int) -> Sample:
        rep_dir = ctx.temp_dir / f"cold-{index}"
        cache_dir, venv_dir = rep_dir / "cache", rep_dir / "venv"
        cache_dir.mkdir(parents=True)
        try:
            return _build_venv(ctx, venv_dir, cache_dir, requirements, offline=False)
        finally:
            venv.rmtree(rep_dir)

    return _run_case(
        ctx, case_id, label, metrics, measure, _venv_attrs(ctx, offline=False)
    )


def case_venv_warm(ctx: _Context) -> CaseResult:
    case_id, label = "venv-warm", "uv venv + install --offline from a cache warmed once"
    metrics = metric_units(_VENV_METRICS)
    if ctx.cfg.no_network:
        return _skipped_case(
            case_id,
            label,
            metrics,
            ctx,
            "--no-network (warming the cache needs one online install)",
        )
    if (gate := _uv_gate(ctx, case_id, label, metrics)) is not None:
        return gate
    requirements = venv.write_requirements(ctx.temp_dir)
    cache_dir = ctx.temp_dir / "warm-cache"
    warm_venv = ctx.temp_dir / "warm-venv"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ctx.log("warming a private uv cache (one online install) ...")
    try:
        setup = _build_venv(ctx, warm_venv, cache_dir, requirements, offline=False)
    except RepError as exc:
        return _failed_case(case_id, label, metrics, ctx, exc)
    ctx.warm_venv, ctx.warm_cache = warm_venv, cache_dir

    def measure(index: int) -> Sample:
        venv_dir = ctx.temp_dir / f"warm-{index}"
        try:
            return _build_venv(ctx, venv_dir, cache_dir, requirements, offline=True)
        finally:
            venv.rmtree(venv_dir)

    attrs = _venv_attrs(ctx, offline=True)
    attrs["warm_setup"] = {
        **setup,
        "note": "the one online install that warmed the cache",
    }
    return _run_case(ctx, case_id, label, metrics, measure, attrs)


def case_venv_start(ctx: _Context) -> CaseResult:
    case_id, label = (
        "venv-start",
        "venv interpreter: import numpy, grpc (process start)",
    )
    metrics = metric_units(["start_to_exit_ms"])
    if ctx.cfg.no_network:
        return _skipped_case(
            case_id, label, metrics, ctx, "--no-network (no warm venv)"
        )
    if (gate := _uv_gate(ctx, case_id, label, metrics)) is not None:
        return gate
    warm = ctx.warm_venv
    if warm is None or not venv.venv_python(warm).exists():
        return _failed_case(
            case_id,
            label,
            metrics,
            ctx,
            RepError(
                "venv_unavailable", "venv-warm setup did not produce a venv", fatal=True
            ),
        )

    def measure(_index: int) -> Sample:
        out = venv.start_process(
            warm, timeout=ctx.budget.slice_for(CONTAINER_TIMEOUT_S)
        )
        if not out.ok:
            raise RepError("process_failed", sanitize_text(out.describe()[:300]))
        if out.stdout.strip() != "ok":
            raise RepError("process_failed", "interpreter printed no 'ok'")
        return {"start_to_exit_ms": out.elapsed_s * 1000.0, "exit_code": out.returncode}

    attrs: dict[str, JSON] = {
        "script": venv.START_SCRIPT,
        **_venv_attrs(ctx, offline=True),
    }
    return _run_case(ctx, case_id, label, metrics, measure, attrs)


CASES: Final[tuple[Callable[[_Context], CaseResult], ...]] = (
    case_docker_start,
    case_docker_pull_cold,
    case_docker_gpu_smi,
    case_docker_gpu_torch,
    case_venv_cold,
    case_venv_warm,
    case_venv_start,
)
CASE_IDS: Final[tuple[str, ...]] = (
    "docker-start",
    "docker-pull-cold",
    "docker-gpu-smi",
    "docker-gpu-torch",
    "venv-cold",
    "venv-warm",
    "venv-start",
)


# ---------------------------------------------------------------------- run
def _cleanup(ctx: _Context) -> list[str]:
    """Remove this run's temp dir and any container with the S6 prefix."""
    leftovers: list[str] = []
    venv.rmtree(ctx.temp_dir)
    if ctx.docker is not None and ctx.docker_facts.get("ok"):
        for name in docker.list_own_containers(ctx.docker):
            docker.force_remove(ctx.docker, name)
        leftovers = docker.list_own_containers(ctx.docker)
    return leftovers


def run_environments(
    cfg: RunConfig, *, log: Callable[[str], None] = lambda _m: None
) -> EnvironmentRun:
    """Measure this machine and return the result envelope."""
    budget = Budget(cfg.effective_budget())
    temp_dir = Path(tempfile.mkdtemp(prefix="s6-env-"))
    cli = None if cfg.skip_docker else docker.find_docker()
    log("collecting docker facts ...")
    dfacts = (
        {"ok": False, "failure_type": SKIPPED, "message": "--skip-docker", "fix": ""}
        if cfg.skip_docker
        else docker.docker_facts(cli)
    )
    uv = venv.find_uv()
    root = dfacts.get("docker_root_dir")
    host = facts.host_facts(
        temp_dir=temp_dir,
        docker_root=root if isinstance(root, str) else None,
        uv_version=venv.uv_version(uv),
    )
    ctx = _Context(
        cfg=cfg,
        budget=budget,
        temp_dir=temp_dir,
        log=log,
        docker=cli,
        docker_facts=dfacts,
        uv=uv,
        host=host,
    )
    if cfg.quick:
        ctx.warnings.append(
            f"QUICK RUN: {cfg.effective_reps()} repetitions per case (< {MIN_VALID_REPS}, "
            "every row INVALID) and no gpu-torch case: a smoke test, not a measurement"
        )
    if not dfacts.get("ok"):
        ctx.warnings.append(
            f"docker: {dfacts.get('failure_type')}: {dfacts.get('message')}"
            + (f" [fix: {dfacts.get('fix')}]" if dfacts.get("fix") else "")
        )
    gpu = host.get("gpu") or {}
    if not gpu.get("present"):
        ctx.warnings.append(f"no NVIDIA GPU on this host: {gpu.get('message')}")
    if uv is None:
        ctx.warnings.append("uv is not on PATH: venv cases cannot run")
    cases: list[CaseResult] = []
    errors: list[dict[str, JSON]] = []
    leftovers: list[str] = []
    try:
        for case_fn, case_id in zip(CASES, CASE_IDS, strict=True):
            if budget.expired():
                errors.append(
                    {
                        "failure_type": "budget_exhausted",
                        "message": f"{case_id} was not started: the {budget.total_s:g} s "
                        "run budget ran out",
                    }
                )
                continue
            log(f"case {case_id} ...")
            cases.append(case_fn(ctx))
    finally:
        leftovers = _cleanup(ctx)
    if leftovers:
        errors.append(
            {
                "failure_type": "container_leak",
                "message": f"containers still present after cleanup: {', '.join(leftovers)}",
            }
        )
    for case in cases:
        if case.case_id in ctx.skipped:
            continue
        errors.extend(
            {
                "failure_type": err.get("failure_type"),
                "message": f"{case.case_id}: {err.get('message')}",
            }
            for err in case.errors
            if err.get("phase") == "setup"
        )
    ctx.warnings += [f"{e['failure_type']}: {e['message']}" for e in errors]
    complete = not errors and all(
        c.case_id in ctx.skipped
        or (not c.aborted and len(c.samples) >= cfg.effective_reps())
        for c in cases
    )
    envelope = make_envelope(
        spike=SPIKE,
        topic=TOPIC,
        link=None,
        host_label=cfg.name,
        params={
            "quick": cfg.quick,
            "reps": cfg.effective_reps(),
            "warmup": cfg.effective_warmup(),
            "budget_s": cfg.effective_budget(),
            "pause_s": cfg.pause_s,
            "skip_docker": cfg.skip_docker,
            "skip_gpu": cfg.skip_gpu,
            "skip_gpu_torch": cfg.skip_gpu_torch,
            "no_network": cfg.no_network,
            "gpu_torch_image": cfg.gpu_torch_image,
        },
        cases=cases,
        errors=errors,
        extra={
            "quick": cfg.quick,
            "facts": host,
            "docker": dfacts,
            "images": ctx.images,
            "pinned_requirements": list(venv.PINNED_REQUIREMENTS),
            "skipped": ctx.skipped,
            "warnings": ctx.warnings,
            "budget_remaining_s": round(budget.remaining(), 3),
            "metric_units": METRIC_UNITS,
            "gpu_torch_cold_pull_note": (
                "docker-gpu-torch.attrs.cold_pull is one informational pull (n=1) "
                "and is never a row of the results table"
            ),
        },
        tool={"script": "lab/spikes/s6_environments.py", "spike": SPIKE},
    )
    return EnvironmentRun(envelope=envelope, complete=complete, warnings=ctx.warnings)
