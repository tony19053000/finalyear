"""Turn a ``StartAttempt`` into a validated plan for one run (V0.4 G4, D52/D67).

The command comes from the network: every field is checked before it is used
(checklist item 9) and a problem is a typed :class:`PlanError` the executor turns
into ``AttemptStatus FAILED`` - never an exception that escapes, never a run.

* ids and the fencing token have their canonical shapes; exactly one of the
  bundle digest and the tree id is set, ``sha256:<64 hex>`` (V0.5 G4: a tree job
  is named by ``tree_id`` alone); the fetch token is bounded and printable;
* ``manifest_json`` is re-validated with the same model the CLI and the
  controller use (:mod:`meshforge.common.manifest`);
* the time limit must not exceed this node's ``[agent.execution] max_timeout_s``;
* GPU indices must be GPUs this node declared in its ``Hello``;
* the runtime is the controller's choice when it names one this node offers,
  else :func:`~.selection.choose_runtime` (docker > venv > subprocess; the venv
  runtime only for the ``python-cpu`` base); none fitting is ``no_feasible_plan``.

**Environment (D51).** The workload gets an explicit allowlist, never the agent's
environment: ``PATH`` (the image's for Docker, the agent's for the subprocess
runtime), ``HOME`` (Docker: ``/tmp``; subprocess: the workdir), ``LANG=C.UTF-8``,
``PYTHONUNBUFFERED=1`` (so output streams live), ``MESHFORGE_JOB_ID`` /
``MESHFORGE_ATTEMPT_ID`` and, on Windows, ``SYSTEMROOT`` (Python cannot start
without it).

**Interpreter.** In Docker, ``python`` is the image's. The subprocess runtime
maps a leading ``python``/``python3`` to the agent's own interpreter
(``sys.executable``) - on Windows there may be no ``python`` on ``PATH``, and the
agent's interpreter is the one whose version the node advertises. The venv
runtime keeps ``python`` and runs it as its cached venv's interpreter.

**Environment (V0.5 D85/D86).** Every plan carries ``RunSpec.env_request``
(manifest ``env``: base, python, requirements); a runtime with an environment
builder resolves it to an environment cached by env hash - built once per node,
reused by every later job with the same inputs. ``setup_argv`` (the V0.4 per-job
``python -m pip install --target <workdir>/.meshforge/deps``) is still planned for
the Docker and subprocess runtimes and used only by a runtime constructed without
a builder.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.agent.execution.docker_runtime import CONTAINER_WORKDIR
from meshforge.agent.execution.runtime import (
    DEPS_SUBDIR,
    EnvRequest,
    RunLimits,
    RunSpec,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import (
    ExecutionOffer,
    GpuNeed,
    choose_runtime,
    runtime_fits_base,
)
from meshforge.agent.settings import ExecutionSettings
from meshforge.common.errors import FailureType, InvalidIdError, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.manifest import (
    MAX_MANIFEST_BYTES,
    Manifest,
    ManifestError,
    parse_manifest_data,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["AttemptPlan", "PlanError", "plan_attempt", "workload_env"]

MAX_FETCH_TOKEN_CHARS: Final = 512
MAX_BLOB_URL_CHARS: Final = 300
_BLOB_URL: Final = re.compile(
    r"https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]{1,253}):\d{1,5}"
)
_DOCKER_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PYTHONS: Final = frozenset({"python", "python3"})
_MIB: Final = 1024 * 1024


class PlanError(MeshForgeError):
    default_failure_type = FailureType.INVALID_INPUT


@dataclass(frozen=True, slots=True)
class AttemptPlan:
    job_id: str
    attempt_id: str
    fencing_token: int
    digest: Digest | None
    """The V0.4 bundle; None for a tree job."""
    fetch_token: str
    blob_url: str
    blob_port: int
    manifest: Manifest
    runtime: str
    spec: RunSpec
    upload_token: str = ""
    """D73 upload grant; empty = this attempt cannot upload (outputs/logs)."""
    upload_max_bytes: int = 0
    upload_max_blobs: int = 0
    tree_id: Digest | None = None
    """V0.5 D83: the tree a tree job stages; None for a bundle job."""


def workload_env(
    *, runtime: str, workdir: Path, job_id: str, attempt_id: str
) -> dict[str, str]:
    """The workload's whole environment (see module docstring)."""
    env = {
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "MESHFORGE_JOB_ID": job_id,
        "MESHFORGE_ATTEMPT_ID": attempt_id,
    }
    if runtime == "docker":
        env["PATH"] = _DOCKER_PATH
        return env
    env["PATH"] = os.environ.get("PATH", "")
    env["HOME"] = str(workdir)
    if sys.platform == "win32":  # pragma: no cover - Windows only
        for name in ("SYSTEMROOT", "TEMP", "TMP"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def _validated_ids(msg: agent_pb2.StartAttempt) -> tuple[str, str, int]:
    try:
        attempt_id = str(validate_attempt_id(msg.attempt_id))
        job_id = str(validate_job_id(msg.job_id))
    except InvalidIdError:
        raise PlanError("StartAttempt ids are not canonical") from None
    if not 1 <= msg.fencing_token < 2**63:
        raise PlanError("StartAttempt.fencing_token is out of range")
    return job_id, attempt_id, int(msg.fencing_token)


def _manifest(text: str) -> Manifest:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_MANIFEST_BYTES:
        raise PlanError("the manifest is over its size cap")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise PlanError("the manifest is not valid JSON") from None
    try:
        return parse_manifest_data(data)
    except ManifestError as exc:
        raise PlanError(f"invalid manifest: {exc.message}") from None


def _argv(manifest: Manifest, runtime: str) -> tuple[str, ...]:
    argv = manifest.entrypoint
    if runtime == "subprocess" and argv[0] in _PYTHONS:
        return (sys.executable, *argv[1:])
    return argv


def _env_request(manifest: Manifest) -> EnvRequest:
    env = manifest.env
    return EnvRequest(base=env.base, python=env.python, requirements=env.requirements)


def _setup_argv(
    manifest: Manifest, runtime: str, workdir: Path
) -> tuple[str, ...] | None:
    requirements = manifest.env.requirements
    if requirements is None or runtime not in ("docker", "subprocess"):
        return None
    if runtime == "docker":
        python, target = "python", f"{CONTAINER_WORKDIR}/{DEPS_SUBDIR}"
        req = f"{CONTAINER_WORKDIR}/{requirements}"
    else:
        python, target = sys.executable, str(workdir / DEPS_SUBDIR)
        req = str(workdir / requirements)
    return (
        python,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--target",
        target,
        "-r",
        req,
    )


def plan_attempt(
    msg: agent_pb2.StartAttempt,
    *,
    settings: ExecutionSettings,
    offer: ExecutionOffer,
    declared_gpus: Collection[int],
    workdir: Path,
    images: Mapping[str, str] | None = None,
) -> AttemptPlan:
    """Validate ``msg`` and build its plan (see module docstring)."""
    job_id, attempt_id, token = _validated_ids(msg)
    spec = msg.spec
    digest, tree_id = _what_to_fetch(spec)
    token_text = spec.fetch_token
    if not token_text or not _token_shape(token_text):
        raise PlanError("ExecutionSpec.fetch_token is missing or malformed")
    if spec.upload_token and not _token_shape(spec.upload_token):
        raise PlanError("ExecutionSpec.upload_token is malformed")
    if spec.blob_url and (
        len(spec.blob_url) > MAX_BLOB_URL_CHARS
        or not _BLOB_URL.fullmatch(spec.blob_url)
    ):
        raise PlanError("ExecutionSpec.blob_url is not http(s)://host:port")
    manifest = _manifest(spec.manifest_json)
    timeout_s = spec.timeout_s or manifest.limits.timeout
    if timeout_s > settings.max_timeout_s:
        raise PlanError(
            f"the job asks for a {timeout_s} s time limit; this node allows at most "
            f"{settings.max_timeout_s:g} s ([agent.execution] max_timeout_s)",
            failure_type=FailureType.NO_FEASIBLE_PLAN,
        )
    gpus = tuple(int(i) for i in spec.gpu_indices)
    if len(set(gpus)) != len(gpus) or any(i not in declared_gpus for i in gpus):
        raise PlanError(
            "ExecutionSpec.gpu_indices names a GPU this node did not declare",
            failure_type=FailureType.CONFIG_ERROR,
        )
    runtime = _runtime(
        spec.runtime, offer, GpuNeed(manifest.resources.gpu), manifest.env.base
    )
    if not runtime.gpu_capable:
        gpus = ()  # "preferred" on a runtime without GPU pass-through: run on CPU
    image_by_base = (
        dict(images)
        if images is not None
        else {
            "python-cpu": settings.images.python_cpu,
            "cuda-pytorch": settings.images.cuda_pytorch,
        }
    )
    memory = spec.memory_bytes or settings.default_memory_mb * _MIB
    run_spec = RunSpec(
        attempt_id=attempt_id,
        argv=_argv(manifest, runtime.name),
        workdir=workdir,
        env=workload_env(
            runtime=runtime.name, workdir=workdir, job_id=job_id, attempt_id=attempt_id
        ),
        limits=RunLimits(
            memory_bytes=max(memory, 6 * _MIB),
            pids=settings.default_pids,
            timeout_s=float(timeout_s),
            cpus=spec.cpus or settings.default_cpus,
        ),
        image=image_by_base[manifest.env.base] if runtime.name == "docker" else None,
        setup_argv=_setup_argv(manifest, runtime.name, workdir),
        gpu_indices=gpus,
        network=manifest.limits.network != "none",
        grace_s=settings.grace_s,
        env_request=_env_request(manifest),
    )
    return AttemptPlan(
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_token=token,
        digest=digest,
        tree_id=tree_id,
        fetch_token=token_text,
        blob_url=spec.blob_url,
        blob_port=int(spec.blob_port),
        manifest=manifest,
        runtime=runtime.name,
        spec=run_spec,
        upload_token=spec.upload_token,
        upload_max_bytes=int(spec.upload_max_bytes),
        upload_max_blobs=int(spec.upload_max_blobs),
    )


def _what_to_fetch(
    spec: agent_pb2.ExecutionSpec,
) -> tuple[Digest | None, Digest | None]:
    """``(bundle, tree)``: exactly one is set (V0.5 G4: explicit tree path).

    A tree job names ``tree_id``; its ``bundle_digest`` is empty (a V0.5 wave-1
    controller sent the tree id there too - accepted only when equal). A V0.4
    bundle job names ``bundle_digest`` and no ``tree_id``.
    """
    if spec.tree_id:
        try:
            tree = Digest.parse(spec.tree_id)
        except (ValueError, MeshForgeError):
            raise PlanError("ExecutionSpec.tree_id is not sha256:<64 hex>") from None
        if spec.bundle_digest and spec.bundle_digest != spec.tree_id:
            raise PlanError("ExecutionSpec names both a bundle and a tree")
        return None, tree
    try:
        return Digest.parse(spec.bundle_digest), None
    except (ValueError, MeshForgeError):
        raise PlanError("ExecutionSpec.bundle_digest is not sha256:<64 hex>") from None


def _token_shape(text: str) -> bool:
    return (
        len(text) <= MAX_FETCH_TOKEN_CHARS
        and text.isprintable()
        and not any(ch.isspace() for ch in text)
    )


def _runtime(
    requested: str, offer: ExecutionOffer, gpu: GpuNeed, base: str
) -> RuntimeAvailability:
    offered = {r.name: r for r in offer.offered()}
    if requested:
        found = offered.get(requested)
        if found is None:
            raise PlanError(
                f"the controller asked for the {requested!r} runtime, which this node "
                "does not offer",
                failure_type=FailureType.NO_FEASIBLE_PLAN,
            )
        if not runtime_fits_base(found, base):
            raise PlanError(
                f"the {requested!r} runtime cannot provide the {base!r} base",
                failure_type=FailureType.NO_FEASIBLE_PLAN,
            )
        return found
    chosen = choose_runtime(offer, gpu, base)
    if chosen is None:
        raise PlanError(
            "no runtime on this node fits the job"
            + (" (it requires a GPU)" if gpu is GpuNeed.REQUIRED else ""),
            failure_type=FailureType.NO_FEASIBLE_PLAN,
        )
    return chosen
