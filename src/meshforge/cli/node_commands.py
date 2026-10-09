"""``compute node prepare NODE_ID|--all`` (V0.5 G5, roadmap 0.5.8 controller part).

Asks the node's agent to pull the pinned base images and build environments
ahead of a run - on a good network, before the lab session - so the first job
neither pulls ``cuda-pytorch`` over Wi-Fi nor installs its requirements.

* ``--image BASE`` (repeatable; default ``python-cpu``): base images to pull.
* ``--envs FILE``: a YAML list of environments to build::

      envs:
        - base: python-cpu          # or cuda-pytorch
          python: "3.12"            # optional
          requirements: requirements.txt   # relative to FILE
          runtime: venv             # optional: docker | venv | subprocess

Waits for every item (``hit`` = already there, ``pulled`` / ``built`` = done
now, ``failed`` with a typed reason) and exits ``FAILURE`` if any failed.
Idempotent: running it again reports ``hit`` for everything already prepared.
REST only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, Final

import typer

from meshforge.cli import app as cli
from meshforge.common.errors import ExitCode

__all__ = ["node_app"]

node_app = typer.Typer(
    name="node", help="Act on one node (or all of them).", no_args_is_help=True
)

_MAX_ENVS_FILE: Final = 1024 * 1024
_POLL_S: Final = 0.5


def _envs(path: Path) -> list[dict[str, str]]:
    import yaml

    try:
        raw = path.read_bytes()
    except OSError as exc:
        cli._die(f"cannot read {path}: {type(exc).__name__}", ExitCode.USAGE_ERROR)
    if len(raw) > _MAX_ENVS_FILE:
        cli._die(f"{path} is over {_MAX_ENVS_FILE} bytes", ExitCode.USAGE_ERROR)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        cli._die(f"{path} is not valid YAML", ExitCode.USAGE_ERROR)
    listed = data.get("envs") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        cli._die(f"{path} must have an 'envs:' list", ExitCode.USAGE_ERROR)
    out: list[dict[str, str]] = []
    for env in listed:
        if not isinstance(env, dict):
            cli._die("every env is a mapping", ExitCode.USAGE_ERROR)
        entry = {"base": str(env.get("base", "python-cpu"))}
        if env.get("python") is not None:
            entry["python"] = str(env["python"])
        if env.get("runtime"):
            entry["runtime"] = str(env["runtime"])
        req = env.get("requirements")
        if req:
            req_path = (path.parent / str(req)).resolve()
            try:
                entry["requirements"] = req_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                cli._die(
                    f"cannot read the requirements file {req}: {type(exc).__name__}",
                    ExitCode.USAGE_ERROR,
                )
        out.append(entry)
    return out


def _line(item: dict[str, Any]) -> str:
    name = item.get("name") or "-"
    what = f"{item.get('kind')} {name}"
    state = str(item.get("state"))
    took = item.get("duration_s") or 0
    text = f"  {what}: {state}"
    if state == "failed":
        text += (
            f" ({item.get('failure_type')}/{item.get('reason')}: {item.get('detail')})"
        )
    else:
        if item.get("key"):
            text += f" {str(item['key'])[:19]}"
        text += f" in {float(took):.1f} s"
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _wait(client: Any, node_id: str, request_id: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    shown: dict[tuple[str, str], str] = {}
    while True:
        status = cli._fetch(lambda: client.prepare_status(node_id, request_id))
        for item in status.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("kind")), str(item.get("name")))
            if item.get("state") in ("hit", "pulled", "built", "failed") and shown.get(
                key
            ) != item.get("state"):
                shown[key] = str(item.get("state"))
                typer.echo(_line(item))
        if status.get("done"):
            return bool(status.get("ok"))
        if time.monotonic() > deadline:
            cli._die(
                f"{node_id}: still preparing after {timeout_s:g} s "
                f"(request {request_id}; it keeps going on the node)",
                ExitCode.FAILURE,
            )
        time.sleep(_POLL_S)


def _starter(client: Any, target: str, body: dict[str, Any]) -> Any:
    return lambda: client.start_prepare(target, body)


@node_app.command("prepare")
def prepare(
    node_id: Annotated[
        str | None, typer.Argument(metavar="[NODE_ID]", help="The node to prepare.")
    ] = None,
    all_nodes: Annotated[
        bool, typer.Option("--all", help="Every connected node.")
    ] = False,
    image: Annotated[
        list[str] | None,
        typer.Option("--image", help="Base image to pull (repeatable; python-cpu)."),
    ] = None,
    envs: Annotated[
        Path | None,
        typer.Option("--envs", help="YAML file listing environments to build."),
    ] = None,
    timeout_s: Annotated[
        float, typer.Option("--timeout", min=1, help="Seconds to wait per node.")
    ] = 3600.0,
) -> None:
    """Pull base images and build environments on a node ahead of a run."""
    if (node_id is None) == (not all_nodes):
        cli._die("give exactly one of NODE_ID or --all", ExitCode.USAGE_ERROR)
    body: dict[str, Any] = {"images": list(dict.fromkeys(image or ["python-cpu"]))}
    if envs is not None:
        body["envs"] = _envs(envs)
    settings = cli._settings()
    with cli._client(settings) as client:
        if all_nodes:
            targets = _connected(client, "prepare")
        else:
            targets = [cli._checked_node_id(node_id) or ""]
        ok = True
        for target in targets:
            started = cli._fetch(_starter(client, target, body))
            request_id = str(started.get("request_id"))
            typer.echo(f"{target}: preparing (request {request_id})")
            ok = _wait(client, target, request_id, timeout_s) and ok
    if not ok:
        raise typer.Exit(ExitCode.FAILURE)


@node_app.command("gc")
def gc(
    node_id: Annotated[
        str | None, typer.Argument(metavar="[NODE_ID]", help="The node.")
    ] = None,
    all_nodes: Annotated[
        bool, typer.Option("--all", help="Every connected node.")
    ] = False,
) -> None:
    """Ask a node's agent to run a cache GC pass now (its log: agent.gc.pass)."""
    if (node_id is None) == (not all_nodes):
        cli._die("give exactly one of NODE_ID or --all", ExitCode.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        if all_nodes:
            targets = _connected(client, "run GC on")
        else:
            targets = [cli._checked_node_id(node_id) or ""]
        for target in targets:
            cli._fetch(_gc_call(client, target))
            typer.echo(f"{target}: GC pass requested")


@node_app.command("revoke")
def revoke(
    node_id: Annotated[str, typer.Argument(help="The node to revoke (node_...).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Revoke a node (V0.7 D138): its certificate is refused on every plane,
    its live session is cut now and its jobs are retried elsewhere. There is
    no un-revoke: `fabric invite --replace NODE_ID` + `join --force` on it."""
    target = cli._checked_node_id(node_id) or ""
    settings = cli._settings()
    with cli._client(settings) as client:
        outcome = cli._fetch(lambda: client.revoke_node(target))
    if json_output:
        cli._echo_json(outcome)
        return
    if outcome.get("already_revoked"):
        typer.echo(f"{target} was already revoked")
        return
    cut = "session cut" if outcome.get("session_killed") else "not connected"
    typer.echo(f"revoked {target} ({cut})")
    typer.echo(
        "to bring it back: compute fabric invite --replace "
        f"{target}, then `meshforge-agent join --force <code>` on that laptop",
        err=True,
    )


def _connected(client: Any, what: str) -> list[str]:
    """``--all``: the connected nodes' ids; none -> a message and exit
    ``UNAVAILABLE`` (prepare and gc alike)."""
    nodes = cli._fetch(client.nodes)
    targets = [
        str(n.get("node_id"))
        for n in nodes.get("nodes") or []
        if isinstance(n, dict) and n.get("connected") is True
    ]
    if not targets:
        cli._die(f"no connected node to {what}", ExitCode.UNAVAILABLE)
    return targets


def _gc_call(client: Any, target: str) -> Any:
    return lambda: client.node_gc(target)
