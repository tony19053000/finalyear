"""``compute run / jobs / logs / cancel / get`` (V0.4 G5, roadmap 0.4.13, D76).

REST only (and the log WebSocket), like every ``compute`` command. The shared
plumbing - settings, the client, ``_die``, ``_fetch`` - lives in
:mod:`meshforge.cli.app` and is looked up there at call time (the tests patch
``app._client``); ``app`` imports these functions at its end and registers them
on the one Typer application.

``compute run [PATH] (--node ID | --local) [-f]``:

1. read the manifest (``compute.yaml``, or inferred - D46) and pack the project
   into a deterministic bundle (D47) in a temporary folder;
2. ``HEAD`` the bundle; ``PUT`` it only when the controller does not have it;
3. ``POST /v1/jobs`` with an ``Idempotency-Key``: ``--idempotency-key`` or a
   generated one, **printed to stderr** so re-running the same command after a
   network error can pass it back and get the same job, not a second one;
4. with ``--follow``: stream the logs until the job ends and exit with the job's
   outcome (:mod:`.outcome`). Ctrl-C only detaches: the job keeps running and the
   ``compute cancel`` command is printed.

``compute get JOB`` downloads the outputs archive, checks its SHA-256 and
unpacks it hostile-input-safe into ``./outputs/<job_id>/`` (``--dest``); a
non-empty destination is refused unless ``--force``, which swaps the new folder
in only after everything succeeded (a failure leaves the old folder untouched)
and is refused for the working directory, its ancestors, home and a root.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from meshforge.cli import app as cli
from meshforge.common.errors import ExitCode

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["cancel", "get", "jobs", "logs", "run"]

_JOB_STATES = (
    "submitted",
    "planning",
    "queued",
    "preparing",
    "running",
    "collecting",
    "succeeded",
    "failed",
    "cancelled",
    "retrying",
)


def _checked_job_id(value: str) -> str:
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import validate_job_id

    try:
        return str(validate_job_id(value))
    except InvalidIdError:
        cli._die(
            "JOB_ID is not a canonical job_<ULID>; run `compute jobs` to list them",
            ExitCode.USAGE_ERROR,
        )


def _latest_attempt(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    attempts = job.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    ranked = [a for a in attempts if isinstance(a, dict)]
    return max(ranked, key=lambda a: int(a.get("fencing_token") or 0), default=None)


# ------------------------------------------------------------------ run


def run(
    path: Annotated[
        Path,
        typer.Argument(help="The project folder (default: the current folder)."),
    ] = Path(),
    node: Annotated[
        str | None, typer.Option("--node", help="Run on this node (Mode 1).")
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Run on this laptop's own agent.")
    ] = False,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow", "-f", help="Stream the logs; exit with the job's outcome."
        ),
    ] = False,
    name: Annotated[
        str | None, typer.Option("--name", help="Job name (overrides the manifest).")
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key",
            help="Re-submit safely: the same key returns the same job.",
        ),
    ] = None,
) -> None:
    """Submit a project as a job (and optionally follow it)."""
    from meshforge.common.bundle import BundleError, pack_to_file
    from meshforge.common.local_node import LocalNodeError, read_local_node_id
    from meshforge.common.manifest import ManifestError, load_manifest, sanitize_name

    if (node is None) == (not local):
        cli._die("give exactly one of --node NODE_ID or --local", ExitCode.USAGE_ERROR)
    settings = cli._settings()
    if local:
        try:
            node_id = str(read_local_node_id(settings.data_dir))
        except LocalNodeError as exc:
            cli._die(exc.message, ExitCode.CONFIG_ERROR)
    else:
        node_id = cli._checked_node_id(node) or ""
    key = idempotency_key or _new_key()
    if not _key_ok(key):
        cli._die(
            "--idempotency-key must be 1-128 printable ASCII characters",
            ExitCode.USAGE_ERROR,
        )
    project = path.resolve()
    try:
        manifest = load_manifest(project)
    except ManifestError as exc:
        cli._die(f"compute.yaml: {exc.message}", ExitCode.FAILURE)
    body_manifest = manifest.canonical_dict()
    if name is not None:
        body_manifest["name"] = sanitize_name(name)
    with tempfile.TemporaryDirectory(prefix="meshforge-run-") as tmp:
        bundle = Path(tmp) / "project.bundle"
        try:
            packed = pack_to_file(project, bundle)
        except BundleError as exc:
            cli._die(f"cannot pack {path}: {exc.message}", ExitCode.FAILURE)
        with cli._client(settings) as client:
            hex_digest = packed.digest.hex
            if not cli._fetch(lambda: client.has_blob(hex_digest)):
                cli._fetch(lambda: client.put_blob(hex_digest, bundle))
            if idempotency_key is None:
                typer.echo(
                    f"{cli.PROG_NAME}: idempotency key {key} (after a network error, "
                    f"re-run with --idempotency-key {key} to avoid a second job)",
                    err=True,
                )
            created = cli._fetch(
                lambda: client.submit_job(
                    {
                        "manifest": body_manifest,
                        "bundle_digest": str(packed.digest),
                        "placement": {"node_id": node_id, "local": local},
                    },
                    idempotency_key=key,
                )
            )
            job_id = str(created.get("job_id"))
            typer.echo(job_id)
            if not follow:
                return
            _follow_to_exit(client, job_id, settings.cli.timeout_s)


def _new_key() -> str:
    from meshforge.common.ids import new_command_id

    return "cli-" + str(new_command_id()).removeprefix("cmd_")


def _key_ok(key: str) -> bool:
    return 1 <= len(key) <= 128 and all(32 < ord(c) < 127 for c in key)


def _follow_to_exit(client: Any, job_id: str, timeout_s: float) -> None:
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.follow import follow_logs, ws_url
    from meshforge.cli.outcome import job_exit_code

    url = ws_url(client.controller_url, job_id, stream="both")
    try:
        end = follow_logs(
            url,
            auth_header=client.auth_header,
            outputs={"stdout": sys.stdout.buffer, "stderr": sys.stderr.buffer},
            open_timeout_s=timeout_s,
        )
    except KeyboardInterrupt:
        typer.echo(
            f"\n{cli.PROG_NAME}: detached - {job_id} keeps running. "
            f"Cancel it with: compute cancel {job_id}",
            err=True,
        )
        raise typer.Exit(ExitCode.INTERRUPTED) from None
    except ControllerError as exc:
        cli._die(exc.message, exit_code_for(exc))
    state = str(end.get("state"))
    failure = end.get("failure_type")
    code = job_exit_code(
        state,
        None if failure is None else str(failure),
        end.get("exit_code") if isinstance(end.get("exit_code"), int) else None,
    )
    if code != ExitCode.OK:
        typer.echo(
            f"{cli.PROG_NAME}: {job_id} {state}"
            + (f" ({failure})" if failure else "")
            + (
                f", workload exit code {end.get('exit_code')}"
                if end.get("exit_code") is not None
                else ""
            ),
            err=True,
        )
    raise typer.Exit(code)


# ------------------------------------------------------------------ jobs


def jobs(
    state: Annotated[
        str | None,
        typer.Option(
            "--state", help=f"Only jobs in this state ({', '.join(_JOB_STATES)})."
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the controller's raw JSON on one line."),
    ] = False,
) -> None:
    """List recent jobs, newest first."""
    from meshforge.cli.render import jobs_table

    if state is not None and state not in _JOB_STATES:
        cli._die(
            f"--state must be one of: {', '.join(_JOB_STATES)}", ExitCode.USAGE_ERROR
        )
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.jobs(state=state, limit=limit))
    if json_output:
        cli._echo_json(payload)
    else:
        cli._out().print(jobs_table(payload))


# ------------------------------------------------------------------ logs


def logs(
    job_id: Annotated[str, typer.Argument(metavar="JOB_ID")],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep streaming until the job ends.")
    ] = False,
    stream: Annotated[
        str, typer.Option("--stream", help="stdout, stderr or both.")
    ] = "both",
) -> None:
    """Print a job's logs (stdout to stdout, stderr to stderr)."""
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.follow import follow_logs, ws_url

    checked = _checked_job_id(job_id)
    if stream not in ("stdout", "stderr", "both"):
        cli._die("--stream must be stdout, stderr or both", ExitCode.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        if follow:
            try:
                end = follow_logs(
                    ws_url(client.controller_url, checked, stream=stream),
                    auth_header=client.auth_header,
                    outputs={"stdout": sys.stdout.buffer, "stderr": sys.stderr.buffer},
                    open_timeout_s=settings.cli.timeout_s,
                )
            except KeyboardInterrupt:
                raise typer.Exit(ExitCode.INTERRUPTED) from None
            except ControllerError as exc:
                cli._die(exc.message, exit_code_for(exc))
            if end.get("gaps"):
                typer.echo(
                    f"{cli.PROG_NAME}: the live stream had gaps; "
                    f"`compute logs {checked}` shows the stored log",
                    err=True,
                )
            return
        data, headers = cli._fetch(lambda: client.logs(checked, stream=stream))
    if stream == "both":
        split = int(headers.get("x-meshforge-stdout-bytes", len(data)))
        sys.stdout.buffer.write(data[:split])
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(data[split:])
        sys.stderr.buffer.flush()
    else:
        target = sys.stdout.buffer if stream == "stdout" else sys.stderr.buffer
        target.write(data)
        target.flush()


# ---------------------------------------------------------------- cancel


def cancel(job_id: Annotated[str, typer.Argument(metavar="JOB_ID")]) -> None:
    """Cancel a job (idempotent). Prints the job's state."""
    checked = _checked_job_id(job_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.cancel(checked))
    typer.echo(f"{checked} {payload.get('state')}")


# ------------------------------------------------------------------- get


def get(
    job_id: Annotated[str, typer.Argument(metavar="JOB_ID")],
    dest: Annotated[
        Path | None,
        typer.Option("--dest", help="Where to unpack (default ./outputs/<job_id>)."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a non-empty destination.")
    ] = False,
) -> None:
    """Download a finished job's outputs (verified) and unpack them.

    Everything happens in a temporary folder next to the destination (same
    filesystem): download, SHA-256 check, unpack. Only when all of that
    succeeded is an existing destination moved aside and the new folder renamed
    into place - a missing output, a network error or a digest mismatch leaves
    the old folder exactly as it was.
    """
    from meshforge.common.bundle import BundleError, unpack

    checked = _checked_job_id(job_id)
    target = dest if dest is not None else Path("outputs") / checked
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        cli._die(f"{target} exists and is not a folder", ExitCode.USAGE_ERROR)
    if force:
        refused = _unsafe_force_target(target)
        if refused is not None:
            cli._die(
                f"refusing --force: {target} is {refused}; pick a dedicated folder",
                ExitCode.USAGE_ERROR,
            )
    if target.is_dir() and any(target.iterdir()) and not force:
        cli._die(f"{target} is not empty; pass --force to replace it", ExitCode.FAILURE)
    settings = cli._settings()
    with cli._client(settings) as client:
        job = cli._fetch(lambda: client.job(checked))
        attempt = _latest_attempt(job)
        digest = None if attempt is None else attempt.get("output_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            cli._die(
                f"{checked} has no outputs (state {job.get('state')})", ExitCode.FAILURE
            )
        hex_digest = digest.removeprefix("sha256:")
        tmp = _staging_dir(target)
        keep_tmp = False
        try:
            archive = tmp / "outputs.bundle"
            try:
                cli._fetch(lambda: client.download_blob(hex_digest, archive))
                matches = _sha256(archive) == hex_digest
            except OSError as exc:
                cli._die(
                    f"cannot save the outputs to {archive}: {_os_cause(exc)}; "
                    f"{target} was left as it was",
                    ExitCode.FAILURE,
                )
            if not matches:
                cli._die(
                    "the downloaded outputs do not match their digest; nothing was "
                    "unpacked",
                    ExitCode.FAILURE,
                )
            fresh = tmp / "new"
            try:
                stats = unpack(archive, fresh)
            except BundleError as exc:
                cli._die(f"cannot unpack the outputs: {exc.message}", ExitCode.FAILURE)
            except OSError as exc:
                cli._die(
                    f"cannot unpack the outputs into {tmp}: {_os_cause(exc)}; "
                    f"{target} was left as it was",
                    ExitCode.FAILURE,
                )
            try:
                _swap_in(fresh, target, tmp / "old")
            except _StrandedError as exc:
                keep_tmp = True
                cli._die(
                    f"cannot move the outputs into {target} ({_os_cause(exc)}) and "
                    f"cannot put the old folder back; it is safe at {exc.parked}",
                    ExitCode.FAILURE,
                )
            except OSError as exc:
                cli._die(
                    f"cannot move the outputs into {target} ({type(exc).__name__}); "
                    "the existing folder was left as it was",
                    ExitCode.FAILURE,
                )
        finally:
            if not keep_tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    missing = attempt.get("missing_outputs") if attempt else None
    typer.echo(f"{stats.files} files -> {target}")
    if isinstance(missing, list) and missing:
        typer.echo(
            f"{cli.PROG_NAME}: declared outputs that matched nothing: "
            + ", ".join(str(m)[:100] for m in missing[:20]),
            err=True,
        )


def _unsafe_force_target(target: Path) -> str | None:
    """Why ``--force`` must not replace ``target`` (``None`` = it may).

    ``--force`` replaces a whole folder; pointing it at the working directory,
    one of its ancestors, the home folder or a filesystem root is a typo that
    would delete far more than a job's outputs.
    """
    resolved = target.resolve()
    cwd = Path.cwd().resolve()
    if resolved == cwd:
        return "the current directory"
    if resolved.parent == resolved:
        return "a filesystem root"
    if resolved in cwd.parents:
        return "an ancestor of the current directory"
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    if home is not None and resolved == home:
        return "the home folder"
    return None


class _StrandedError(OSError):
    """Both renames of :func:`_swap_in` failed: the old folder is at ``parked``
    and must not be deleted - the caller keeps its staging folder and prints
    the path so the user can move it back by hand."""

    def __init__(self, cause: OSError, parked: Path) -> None:
        super().__init__(cause.errno, cause.strerror or str(cause))
        self.parked = parked


def _staging_dir(target: Path) -> Path:
    """Create the ``.compute-get-*`` staging folder next to ``target`` (same
    filesystem, so the final rename is atomic). An unwritable or missing parent
    is a clean one-line error, never a traceback."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=target.parent, prefix=".compute-get-"))
    except OSError as exc:
        cli._die(
            f"cannot create a staging folder in {target.parent}: {_os_cause(exc)}",
            ExitCode.FAILURE,
        )


def _os_cause(exc: OSError) -> str:
    """The short human cause of an ``OSError`` (``strerror``, else its type)."""
    return exc.strerror or type(exc).__name__


def _swap_in(fresh: Path, target: Path, parked: Path) -> None:
    """Rename ``fresh`` onto ``target``; an existing ``target`` is parked first
    (and put back if the second rename fails). All three share one parent
    filesystem, so both renames are atomic; the caller then deletes its staging
    folder with the parked copy in it. If putting the old folder back fails
    too, :class:`_StrandedError` names where it is and the caller must keep it."""
    if not target.exists():
        fresh.rename(target)
        return
    target.rename(parked)
    try:
        fresh.rename(target)
    except OSError as exc:
        try:
            parked.rename(target)
        except OSError:
            raise _StrandedError(exc, parked) from exc
        raise


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()
