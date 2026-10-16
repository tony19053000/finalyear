"""``compute run / jobs / logs / cancel / get`` (V0.4 G5, roadmap 0.4.13, D76).

REST only (and the log WebSocket), like every ``compute`` command. The shared
plumbing - settings, the client, ``_die``, ``_fetch`` - lives in
the leaf module :mod:`meshforge.cli.shared` and is looked up there at call time
(the tests patch ``shared._client``); :mod:`meshforge.cli.app` imports these
functions at its end and registers them on the one Typer application.

``compute run [PATH] [--node ID | --local] [--policy P] [--priority N] [-f]``
(V0.6: without --node/--local the controller's planner places the job; this
laptop's own enrolled agent, if any, is sent as ``submitter_node_id`` - the
Local candidate):

1. read the manifest (``compute.yaml``, or inferred - D46) and build the
   project's tree manifest (V0.5 D81: one blob per file, same ignore rules as
   bundles);
2. upload only what the controller lacks (:mod:`.transfer`: have/need, large
   files in resumable parts) and print ``uploaded N files / X bytes, skipped M``
   to stderr - an unchanged project uploads nothing; then (V0.6 D109), when the
   job may run on this laptop's own agent, copy the project into that agent's
   cache too (``seeded: true``; skipped with a logged reason when it cannot);
3. ``POST /v1/jobs`` with the ``tree_id`` and an ``Idempotency-Key``: ``--idempotency-key`` or a
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
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit
from meshforge.common.errors import FailureType

_WORD = re.compile(r"[a-z_]{1,32}")
"""A failure type / state name from the controller, printed as is; anything
else (it comes from the network) is shown as ``?``."""

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
            CliExit.USAGE_ERROR,
        )


def _print_retry(frame: Mapping[str, Any]) -> None:
    """V0.6 G3: the followed attempt ended and the job was retried."""
    sys.stdout.flush()
    why = frame.get("previous_failure_type") or frame.get("previous_state")
    word = why if isinstance(why, str) and _WORD.fullmatch(why) else "?"
    token = frame.get("fencing_token")
    number = token if isinstance(token, int) and not isinstance(token, bool) else "?"
    render.note(
        f"the attempt ended ({word}); the job was retried - following attempt {number}"
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
        str | None,
        typer.Option("--node", help="Pin the job to this node (Mode 1)."),
    ] = None,
    local: Annotated[
        bool,
        typer.Option("--local", help="Pin the job to this laptop's own agent."),
    ] = False,
    policy: Annotated[
        str | None,
        typer.Option(
            "--policy",
            help="Placement policy for an automatic job: random, round_robin, "
            "least_loaded, strongest_suitable, resource_score, cache_affinity, "
            "cost_model (default: the manifest's, else the controller's).",
        ),
    ] = None,
    priority: Annotated[
        int | None,
        typer.Option(
            "--priority",
            min=-10,
            max=10,
            help="Queue priority -10..10 (overrides the manifest's).",
        ),
    ] = None,
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
    """Submit a project as a job (and optionally follow it).

    Without --node/--local the controller's planner chooses the node (V0.6);
    when this laptop runs an enrolled agent, it is the Local candidate.
    """
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.transfer import TransferError, upload_tree
    from meshforge.common.errors import FailureType
    from meshforge.common.manifest import (
        POLICIES,
        ManifestError,
        load_manifest,
        sanitize_name,
    )
    from meshforge.common.tree import TreeError, build_tree

    if node is not None and local:
        cli._die("give at most one of --node NODE_ID and --local", CliExit.USAGE_ERROR)
    if policy is not None and policy not in POLICIES:
        cli._die("--policy must be one of " + ", ".join(POLICIES), CliExit.USAGE_ERROR)
    settings = cli._settings()
    own_node = _own_node_id(settings.data_dir, required=local)
    node_id = own_node if local else cli._checked_node_id(node)
    key = idempotency_key or _new_key()
    if not _key_ok(key):
        cli._die(
            "--idempotency-key must be 1-128 printable ASCII characters",
            CliExit.USAGE_ERROR,
        )
    project = path.resolve()
    try:
        manifest = load_manifest(project)
    except ManifestError as exc:
        cli._die(f"compute.yaml: {exc.message}", CliExit.FAILURE)
    body_manifest = manifest.canonical_dict()
    if name is not None:
        body_manifest["name"] = sanitize_name(name)
    if priority is not None:
        body_manifest["priority"] = priority
    try:
        built = build_tree(project)
    except (TreeError, OSError) as exc:
        reason = exc.message if isinstance(exc, TreeError) else type(exc).__name__
        cli._die(f"cannot read {path}: {reason}", CliExit.FAILURE)
    placement: dict[str, Any] = {}
    if node_id is not None:
        placement = {"node_id": node_id, "local": local}
    if policy is not None:
        placement["policy"] = policy
    body: dict[str, Any] = {
        "manifest": body_manifest,
        "tree_id": str(built.tree.tree_id),
        "placement": placement,
    }
    if own_node is not None:
        body["submitter_node_id"] = own_node
    with cli._client(settings) as client:
        try:
            report = cli._fetch(lambda: upload_tree(client, built))
        except (TransferError, OSError) as exc:
            reason = (
                exc.message if isinstance(exc, TransferError) else type(exc).__name__
            )
            cli._die(f"upload failed: {reason}", CliExit.FAILURE)
        render.note(
            f"uploaded {report.uploaded_blobs} files / "
            f"{report.uploaded_bytes} bytes, skipped {report.skipped_blobs}"
            + (
                f" (resumed {report.resumed_parts} parts)"
                if report.resumed_parts
                else ""
            )
        )
        if own_node is not None and (node_id is None or node_id == own_node):
            # D109: the Local candidate needs no download if its cache holds
            # the project already.
            body["seeded"] = _seed(settings, built)
        if idempotency_key is None:
            render.note(
                f"idempotency key {key} (after a network error, "
                f"re-run with --idempotency-key {key} to avoid a second job)"
            )
        try:
            created = client.submit_job(body, idempotency_key=key)
        except ControllerError as exc:
            if exc.failure_type is FailureType.NO_FEASIBLE_PLAN:
                _print_rejections(exc.details.get("rejections"))
            cli._die(exc.message, exit_code_for(exc))
        job_id = str(created.get("job_id"))
        render.line(render.echo(job_id))
        if not follow:
            return
        _follow_to_exit(client, job_id, settings.cli.timeout_s)


def _seed(settings: Any, built: Any) -> bool:
    """V0.6 D109: copy the project into this laptop's agent cache (best effort;
    the reason for a skip is logged, the job is submitted either way)."""
    from meshforge.cli.transfer import seed_local
    from meshforge.common.logging import configure_logging

    cfg = settings.cli
    if not cfg.seed_local:
        return False
    # The cache and GC-index modules log through structlog; stdout is the job
    # id only (scripts read it), so their warnings go to stderr.
    configure_logging("WARNING", fmt="console", component="cli", stream=sys.stderr)
    report = seed_local(
        built,
        settings.data_dir,
        max_bytes=cfg.seed_max_bytes,
        free_disk_floor_bytes=cfg.seed_free_disk_floor_mb * 1024 * 1024,
    )
    if report.seeded:
        render.note(
            f"local agent cache seeded ({report.blobs_written} "
            f"new, {report.blobs_present} already there)"
        )
    else:
        render.note(
            f"local agent cache not seeded ({report.reason}); "
            "a job placed here downloads the project"
        )
    return report.seeded


def _own_node_id(data_dir: Path, *, required: bool) -> str | None:
    """This laptop's enrolled node (D103): required for --local; otherwise
    best effort - no agent here simply means no Local candidate."""
    from meshforge.common.local_node import LocalNodeError, read_local_node_id

    try:
        return str(read_local_node_id(data_dir))
    except LocalNodeError as exc:
        if required:
            cli._die(exc.message, CliExit.CONFIG_ERROR)
        return None


def _print_rejections(rejections: object) -> None:
    """D104: why no node could ever run the job, one node per line."""
    if not isinstance(rejections, list):
        return
    if not rejections:
        render.note("no node has joined the fabric")
    for item in rejections:
        if isinstance(item, dict):
            render.note(
                f"  {render.echo(item.get('node_id'))}: "
                f"{render.echo(item.get('reject_code'))} - {render.echo(item.get('reject_text'))}"
            )


def _new_key() -> str:
    from meshforge.common.ids import new_command_id

    return "cli-" + str(new_command_id()).removeprefix("cmd_")


def _key_ok(key: str) -> bool:
    return 1 <= len(key) <= 128 and all(32 < ord(c) < 127 for c in key)


def _follow_failed_hint(job_id: str, failure_type: FailureType) -> str:
    """The advice line after ``compute run -f`` lost (or never got) the log
    stream of a job it did submit.

    Usually: follow it again with ``compute logs -f``. Not when the controller
    refused the login (UNAUTHORIZED, D202): ``compute logs -f`` would be refused
    the same way, so the line says what happened, why, and to log in again -
    with no ``logs -f`` suggestion that cannot work yet.
    """
    if failure_type is FailureType.UNAUTHORIZED:
        return (
            f"{job_id} was submitted and keeps running, but the fabric refused "
            "this login for its log stream (expired, revoked, or not allowed) - "
            "log in again with `compute login`, then check on it with `compute jobs`"
        )
    return (
        f"{job_id} was submitted and keeps running - follow it with "
        f"`compute logs -f {job_id}`, or cancel it with `compute cancel {job_id}`"
    )


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
            on_retry=_print_retry,
            ssl_context=client.ssl_context,
        )
    except KeyboardInterrupt:
        render.line(err=True)  # end the half-printed log line first
        render.note(
            f"detached - {job_id} keeps running. "
            f"Cancel it with: compute cancel {job_id}"
        )
        raise typer.Exit(CliExit.INTERRUPTED) from None
    except ControllerError as exc:
        # The job was submitted: losing (or never getting) its log stream does
        # not stop it, and the exit code is ours, not the job's.
        render.fail(exc.message)
        render.note(_follow_failed_hint(job_id, exc.failure_type))
        raise typer.Exit(exit_code_for(exc)) from None
    state = str(end.get("state"))
    failure = end.get("failure_type")
    code = job_exit_code(
        state,
        None if failure is None else str(failure),
        end.get("exit_code") if isinstance(end.get("exit_code"), int) else None,
    )
    if code != CliExit.OK:
        render.note(
            f"{render.echo(job_id)} {render.echo(state)}"
            + (f" ({render.echo(failure)})" if failure else "")
            + (
                f", workload exit code {render.echo(end.get('exit_code'))}"
                if end.get("exit_code") is not None
                else ""
            )
        )
    raise typer.Exit(code)


# ------------------------------------------------------------------ jobs


def jobs(
    job_id: Annotated[
        str | None,
        typer.Argument(
            metavar="[JOB_ID]",
            help="Show one job: attempts, named inputs, transfer telemetry.",
        ),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option(
            "--state", help=f"Only jobs in this state ({', '.join(_JOB_STATES)})."
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    json_output: cli.JsonOption = False,
) -> None:
    """List recent jobs, newest first (or show one job in detail)."""
    from meshforge.cli.render import job_detail_table, jobs_table

    if job_id is not None:
        checked = _checked_job_id(job_id)
        settings = cli._settings()
        with cli._client(settings) as client:
            detail = cli._fetch(lambda: client.job(checked))
        if json_output:
            cli._echo_json(detail)
        else:
            render.show(job_detail_table(detail))
        return

    if state is not None and state not in _JOB_STATES:
        cli._die(
            f"--state must be one of: {', '.join(_JOB_STATES)}", CliExit.USAGE_ERROR
        )
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.jobs(state=state, limit=limit))
    if json_output:
        cli._echo_json(payload)
    else:
        render.show(jobs_table(payload))


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
        cli._die("--stream must be stdout, stderr or both", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        if follow:
            try:
                end = follow_logs(
                    ws_url(client.controller_url, checked, stream=stream),
                    auth_header=client.auth_header,
                    outputs={"stdout": sys.stdout.buffer, "stderr": sys.stderr.buffer},
                    open_timeout_s=settings.cli.timeout_s,
                    on_retry=_print_retry,
                    ssl_context=client.ssl_context,
                )
            except KeyboardInterrupt:
                raise typer.Exit(CliExit.INTERRUPTED) from None
            except ControllerError as exc:
                cli._die(exc.message, exit_code_for(exc))
            if end.get("gaps"):
                render.note(
                    f"the live stream had gaps; "
                    f"`compute logs {checked}` shows the stored log"
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
    render.line(f"{checked} {render.echo(payload.get('state'))}")


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
        cli._die(f"{target} exists and is not a folder", CliExit.USAGE_ERROR)
    if force:
        refused = _unsafe_force_target(target)
        if refused is not None:
            cli._die(
                f"refusing --force: {target} is {refused}; pick a dedicated folder",
                CliExit.USAGE_ERROR,
            )
    if target.is_dir() and any(target.iterdir()) and not force:
        cli._die(f"{target} is not empty; pass --force to replace it", CliExit.FAILURE)
    settings = cli._settings()
    with cli._client(settings) as client:
        job = cli._fetch(lambda: client.job(checked))
        attempt = _latest_attempt(job)
        digest = None if attempt is None else attempt.get("output_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            cli._die(
                f"{checked} has no outputs (state {job.get('state')})", CliExit.FAILURE
            )
        hex_digest = digest.removeprefix("sha256:")
        tmp = _staging_dir(target)
        parked = tmp / "old"
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
                    CliExit.FAILURE,
                )
            if not matches:
                cli._die(
                    "the downloaded outputs do not match their digest; nothing was "
                    "unpacked",
                    CliExit.FAILURE,
                )
            fresh = tmp / "new"
            try:
                stats = unpack(archive, fresh)
            except BundleError as exc:
                cli._die(f"cannot unpack the outputs: {exc.message}", CliExit.FAILURE)
            except OSError as exc:
                cli._die(
                    f"cannot unpack the outputs into {tmp}: {_os_cause(exc)}; "
                    f"{target} was left as it was",
                    CliExit.FAILURE,
                )
            try:
                _swap_in(fresh, target, parked)
            except _StrandedError as exc:
                keep_tmp = True
                cli._die(
                    f"cannot move the outputs into {target} ({_os_cause(exc)}) and "
                    f"cannot put the old folder back; it is safe at {exc.parked}",
                    CliExit.FAILURE,
                )
            except OSError as exc:
                cli._die(
                    f"cannot move the outputs into {target} ({_os_cause(exc)}); "
                    "the existing folder was left as it was",
                    CliExit.FAILURE,
                )
        finally:
            if not keep_tmp and _parked_only(parked, target):
                # interrupted (Ctrl-C, any non-OSError) between parking the old
                # folder and putting something back at ``target``: never delete it
                keep_tmp = True
                render.note(
                    f"interrupted while replacing {target}; the old "
                    f"folder is safe at {parked}"
                )
            if not keep_tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    missing = attempt.get("missing_outputs") if attempt else None
    render.line(f"{stats.files} files -> {target}")
    if isinstance(missing, list) and missing:
        render.note(
            "declared outputs that matched nothing: "
            + ", ".join(str(m)[:100] for m in missing[:20])
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
        super().__init__(cause.errno, cause.strerror or str(cause), cause.filename)
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
            CliExit.FAILURE,
        )


def _os_cause(exc: OSError) -> str:
    """The short human cause of an ``OSError`` (``strerror``, else its type)."""
    return exc.strerror or type(exc).__name__


def _parked_only(parked: Path, target: Path) -> bool:
    """The old folder sits at ``parked`` and nothing is at ``target``: deleting
    the staging folder now would lose the user's only copy."""
    return parked.is_dir() and not os.path.lexists(target)


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
