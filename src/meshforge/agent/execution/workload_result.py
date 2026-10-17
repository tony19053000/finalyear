"""Read a benchmark's ``results/workload.json`` for the terminal report (V0.9 G1, D213).

A benchmark job (``meshforge.bench``) measures its own kernel with a monotonic
clock and writes ``compute_ns`` - plus its family, parameters, seed and status -
to ``results/workload.json`` in its working directory. After the workload
exits, the executor reads that file **once** and puts it in the terminal
``AttemptStatus``: ``PhaseTimings.compute_ns`` and a ``WorkloadReport``.

The file is written by the workload, so it is **untrusted input**:

* opened without following a symlink, on every path component (``results`` and
  the file: ``O_NOFOLLOW`` through a directory descriptor where the OS has
  them; on Windows a final ``lstat`` check) - a workload cannot make the agent
  read a file outside its workdir;
* at most :data:`MAX_RESULT_BYTES` read; anything else (not JSON, not an object,
  wrong types) means "no report", never an error for the attempt;
* every field bounded (:data:`MAX_FAMILY_CHARS`, :data:`MAX_PARAMS_JSON_CHARS`,
  a known status, ``compute_ns`` within 8 days).

Any job may write such a file; for a non-benchmark job it is simply absent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import (
    RedactionStallError,
    SecretMatcher,
    redact_bytes,
)

__all__ = [
    "MAX_PARAMS_JSON_CHARS",
    "MAX_RESULT_BYTES",
    "RESULT_PATH",
    "WorkloadReport",
    "read_workload_result",
]

RESULT_PATH: Final = ("results", "workload.json")
MAX_RESULT_BYTES: Final = 256 * 1024
MAX_FAMILY_CHARS: Final = 64
MAX_PARAMS_JSON_CHARS: Final = 4096
MAX_COMPUTE_NS: Final = 8 * 24 * 3600 * 10**9
STATUSES: Final = frozenset({"ok", "check_failed", "unavailable", "bad_params"})
_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_MAX_SEED: Final = 2**64 - 1


@dataclass(frozen=True, slots=True)
class WorkloadReport:
    family: str
    params_json: str
    """Canonical JSON (sorted keys, compact); "" when absent or too large."""
    seed: int | None
    status: str
    device: str
    compute_ns: int | None
    result_schema_version: int

    def to_proto(self) -> agent_pb2.WorkloadReport:
        msg = agent_pb2.WorkloadReport(
            family=self.family,
            params_json=self.params_json,
            status=self.status,
            device=self.device,
            result_schema_version=self.result_schema_version,
        )
        if self.seed is not None:
            msg.seed = self.seed
        return msg


def read_workload_result(
    workdir: Path, *, matcher: SecretMatcher | None = None
) -> WorkloadReport | None:
    """The report in ``workdir/results/workload.json``, or None (absent,
    unreadable, malformed). Blocking: call it in a thread.

    ``matcher`` is the attempt's secret matcher (V0.7 G6): a text field in
    which it finds one of the job's secret values (in any encoded form the
    log redaction knows) is **dropped** - the report reaches the controller,
    the run record and the admin's dataset export, so a workload must not be
    able to smuggle its secrets out through it."""
    raw = _read_bounded(workdir)
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    report = _report(document)
    if report is None or matcher is None or not matcher:
        return report
    return _without_secrets(report, matcher)


def _without_secrets(report: WorkloadReport, matcher: SecretMatcher) -> WorkloadReport:
    def clean(value: str) -> str:
        data = value.encode("utf-8")
        try:
            return value if redact_bytes(data, matcher) == data else ""
        except RedactionStallError:
            return ""  # fail closed: the field is dropped

    return dataclasses.replace(
        report,
        family=clean(report.family),
        params_json=clean(report.params_json),
        device=clean(report.device),
    )


def _read_bounded(workdir: Path) -> bytes | None:
    directory, name = RESULT_PATH
    if _NOFOLLOW and _DIRECTORY and os.open in os.supports_dir_fd:
        return _read_posix(workdir, directory, name)
    return _read_portable(workdir / directory / name)


def _read_posix(workdir: Path, directory: str, name: str) -> bytes | None:
    try:
        root = os.open(workdir, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            sub = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=root)
        except OSError:
            return None
        try:
            try:
                fd = os.open(
                    name,
                    os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=sub,
                )
            except OSError:
                return None
            return _read_fd(fd)
        finally:
            os.close(sub)
    finally:
        os.close(root)


def _read_portable(path: Path) -> bytes | None:
    """Windows: no ``dir_fd``; refuse a symlinked (or junctioned) component."""
    try:
        for part in (path.parent, path):
            info = os.lstat(part)
            if stat.S_ISLNK(info.st_mode) or getattr(part, "is_junction", bool)():
                return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    return _read_fd(fd)


def _read_fd(fd: int) -> bytes | None:
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            data = handle.read(MAX_RESULT_BYTES + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
    return None if len(data) > MAX_RESULT_BYTES else data


def _report(document: dict[str, Any]) -> WorkloadReport | None:
    family = document.get("family")
    status = document.get("status")
    if not isinstance(family, str) or not _NAME.match(family):
        family = ""
    if status not in STATUSES:
        return None
    params = document.get("params")
    params_json = ""
    if isinstance(params, dict):
        try:
            text = json.dumps(
                params, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError):
            text = ""
        params_json = text if len(text) <= MAX_PARAMS_JSON_CHARS else ""
    device = document.get("device")
    version = _bounded_int(document.get("schema_version"), 1, 2**31 - 1)
    return WorkloadReport(
        family=family,
        params_json=params_json,
        seed=_bounded_int(document.get("seed"), 0, _MAX_SEED),
        status=str(status),
        device=device if isinstance(device, str) and _NAME.match(device) else "",
        compute_ns=_compute_ns(document.get("compute_ns")),
        result_schema_version=0 if version is None else version,
    )


def _bounded_int(value: object, low: int, high: int) -> int | None:
    """``value`` when it is an int (never a bool) in ``[low, high]``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _compute_ns(value: object) -> int | None:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    return _bounded_int(value, 0, MAX_COMPUTE_NS)
