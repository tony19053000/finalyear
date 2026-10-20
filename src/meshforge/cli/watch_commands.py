"""``compute watch JOB`` - a split job's progress until it ends (V0.10 G7,
D270); also what ``compute run -f`` shows for a ``split:`` job unless
``--no-progress``. Rendering and the poll loop: :mod:`.split_watch`.

Exit code = the job's outcome (:func:`meshforge.cli.exits.job_exit_code`),
like ``compute run -f``. ``--json`` prints the progress once and exits 0 (the
``--json`` contract of every read command: one request, one line). Ctrl-C
only detaches (the job keeps running). A job that is not a split job is
refused (``compute logs -f`` follows those).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit

__all__ = ["watch", "watch_to_exit"]


def watch_to_exit(client: Any, job_id: str, *, interval_s: float) -> None:
    """Show the progress until the job ends, then exit with its outcome."""
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.exits import job_exit_code
    from meshforge.cli.split_watch import watch_split

    try:
        final = watch_split(
            lambda: client.split_progress(job_id),
            interval_s=interval_s,
            transient=_transient,
        )
    except KeyboardInterrupt:
        render.line(err=True)
        render.note(
            f"detached - {job_id} keeps running. "
            f"Cancel it with: compute cancel {job_id}"
        )
        raise typer.Exit(CliExit.INTERRUPTED) from None
    except ControllerError as exc:
        render.fail(exc.message)
        render.note(
            f"{job_id} keeps running - watch it again with `compute watch {job_id}`"
        )
        raise typer.Exit(exit_code_for(exc)) from None
    detail = _detail(client, job_id)
    state = str(final.get("job_state"))
    failure = detail.get("failure_type")
    if state != "succeeded":
        render.note(
            f"{render.echo(job_id)} {render.flat(state)}"
            + (f" ({render.echo(failure)})" if failure else "")
        )
    for text in render.warning_lines(detail):
        render.note(f"warning: {render.flat(text)}")
    raise typer.Exit(
        job_exit_code(state, None if failure is None else str(failure), None)
    )


def _transient(exc: Exception) -> bool:
    """A network blip or a busy controller: poll again (bounded)."""
    from meshforge.cli.client import ControllerError
    from meshforge.common.errors import FailureType

    if isinstance(exc, OSError):
        return True
    return isinstance(exc, ControllerError) and exc.failure_type in (
        FailureType.NETWORK_ERROR,
        FailureType.TIMEOUT,
    )


def _detail(client: Any, job_id: str) -> Mapping[str, Any]:
    """The ended job (failure type, warnings); best effort - ``{}`` if the
    read fails (the exit code then follows the state alone)."""
    from meshforge.cli.client import ControllerError

    try:
        detail = client.job(job_id)
    except (ControllerError, OSError):
        return {}
    return detail if isinstance(detail, Mapping) else {}


def watch(
    job_id: Annotated[str, typer.Argument(metavar="JOB_ID")],
    json_output: cli.JsonOption = False,
    interval: Annotated[
        float | None,
        typer.Option(
            "--interval",
            min=0.5,
            max=60.0,
            help="Seconds between polls (default [cli] progress_interval_s).",
        ),
    ] = None,
) -> None:
    """Watch a split job's progress per chunk and per node until it ends
    (--json: print the progress once, for scripts)."""
    from meshforge.cli.job_commands import _checked_job_id

    checked = _checked_job_id(job_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        first = cli._fetch(lambda: client.split_progress(checked))  # 404: not split
        if json_output:
            cli._echo_json(first)
            return
        watch_to_exit(
            client,
            checked,
            interval_s=interval or settings.cli.progress_interval_s,
        )
