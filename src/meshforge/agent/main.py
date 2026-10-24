"""Process entry point for ``meshforge-agent`` (roadmap 0.3.8 E, ADR-0013).

Commands::

    meshforge-agent run [--config FILE] [--controller HOST:PORT]
    meshforge-agent identity show [--config FILE] [--json]
    meshforge-agent identity reset --yes [--config FILE]
    meshforge-agent join [CODE|-] [--force] [--config FILE]   (V0.7 G2)
    meshforge-agent hardware show [--config FILE] [--json]
    meshforge-agent hardware score [--config FILE] [--json] [--force]
    meshforge-agent prepare [--config FILE] [--base NAME]... [--timeout S] [--json]
    meshforge-agent policy show|set KEY=VALUE...|reset [--config FILE]   (V0.7 G5)
    meshforge-agent migrate-identity [--from DIR] [--config-from FILE] [--dry-run]
                                                               (V0.17 G1, root)

Everything beyond ``--version``/``--help`` is imported inside the command so the
module runs on a base install (no agent extra). ``run`` itself needs only core
dependencies when ``controller_address`` is configured; mDNS discovery needs the
``agent`` extra (``zeroconf``) and says so if it is missing.

``hardware show`` and ``hardware score`` are **local only** (brief §5e D13,
§5g D34): they never contact the controller. The controller receives the clipped
``Hello.hardware`` summary and nothing else, so per-probe status, duration and
reason can only be seen here.

``prepare`` (V0.5 D87) is local too: it pre-pulls the digest-pinned base images
of ``[agent.execution.images]`` and prints their sizes and pull times.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from meshforge.agent.hardware import HardwareFacts
    from meshforge.agent.settings import AgentSettings

PROG_NAME = "meshforge-agent"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge node agent process.",
    add_completion=False,
    no_args_is_help=True,
)
identity_app = typer.Typer(
    name="identity",
    help="Inspect or reset this node's identity (node_id + keypair, ADR-0013).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(identity_app, name="identity")
hardware_app = typer.Typer(
    name="hardware",
    help="Inspect the hardware this node discovers (local only; D13).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(hardware_app, name="hardware")


def _add_policy_app() -> None:
    from meshforge.agent.policy_cli import bench_app, policy_app

    app.add_typer(policy_app, name="policy")
    app.add_typer(bench_app, name="bench")  # V0.9 G5: owner's benchmark switch


_add_policy_app()

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="TOML settings file (default: $MESHFORGE_CONFIG or <config dir>/config.toml).",
        exists=False,
        dir_okay=False,
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        from meshforge.common.buildinfo import version_line

        typer.echo(version_line(PROG_NAME))
        raise typer.Exit(ExitCode.OK)


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """MeshForge node agent process."""


def _load_settings(config: Path | None, **overrides: object) -> AgentSettings:
    from meshforge.agent.settings import AgentSettings
    from meshforge.common.errors import MeshForgeError

    try:
        return AgentSettings.load(config, **overrides)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None


_LOCAL_ONLY: dict[str, object] = {"agent": {"discovery": {"enabled": True}}}
"""Overrides for commands that never contact the controller (identity, join):
they need only the data dir, so "no way to find the controller" must not stop
them on a laptop that has not been told where it is yet."""


def _quiet_logging(settings: AgentSettings) -> None:
    """For the ``identity`` commands: stdout is the result, logs go to stderr and
    only warnings and worse (an unconfigured structlog would print to stdout)."""
    from meshforge.common.logging import configure_logging

    configure_logging("WARNING", fmt=settings.log.format, component="agent")


@app.command()
def run(
    config: ConfigOption = None,
    controller: Annotated[
        str | None,
        typer.Option(
            "--controller",
            help="Controller host:port (same as agent.controller_address); "
            "omit to discover it via mDNS.",
        ),
    ] = None,
) -> None:
    """Start the node agent: connect to the controller and stay connected."""
    from meshforge.common.logging import configure_logging

    overrides: dict[str, object] = {}
    if controller is not None:
        overrides["agent"] = {"controller_address": controller}
    settings = _load_settings(config, **overrides)
    configure_logging(settings.log.level, fmt=settings.log.format, component="agent")

    from meshforge.agent.runtime import run_agent

    raise typer.Exit(run_agent(settings))


@identity_app.command("show")
def identity_show(
    config: ConfigOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Print node_id, session_epoch, the key fingerprint and - once joined - the
    fabric, certificate expiry and fingerprints (never the key)."""
    from meshforge.agent.enrollment import JoinError, load
    from meshforge.agent.identity import load_or_create
    from meshforge.common.errors import MeshForgeError

    settings = _load_settings(config, **_LOCAL_ONLY)
    _quiet_logging(settings)
    try:
        identity = load_or_create(settings.identity_dir)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    facts: dict[str, object] = {
        "node_id": str(identity.node_id),
        "session_epoch": identity.session_epoch,
        "key_fingerprint_sha256": identity.fingerprint,
        "path": str(identity.path),
    }
    problem: JoinError | None = None
    try:
        enrollment = load(identity)
    except JoinError as exc:
        enrollment, problem = None, exc
    if enrollment is not None:
        from datetime import UTC, datetime

        from meshforge.agent.identity import staged_rotation_key
        from meshforge.agent.session.renewal import renewal_due_at

        try:
            rotation_staged = staged_rotation_key(identity.path) is not None
        except MeshForgeError as exc:
            rotation_staged = False
            facts["rotation_problem"] = exc.message
        facts.update(
            enrolled=True,
            fabric_id=enrollment.fabric_id,
            cert_serial=format(enrollment.node_cert.serial_number, "x"),
            cert_not_after=enrollment.not_after.isoformat(),
            cert_expired=enrollment.not_after <= datetime.now(UTC),
            # V0.7 G4 (D140): when the running agent renews it.
            cert_renewal_due=renewal_due_at(
                enrollment.node_cert.not_valid_before_utc, enrollment.not_after
            ).isoformat(),
            key_rotation_staged=rotation_staged,
            cert_fingerprint_sha256=enrollment.cert_fingerprint,
            ca_fingerprint_sha256=enrollment.ca_fingerprint,
        )
    else:
        facts["enrolled"] = False
        if problem is not None:
            facts["enrollment_problem"] = problem.message
    if as_json:
        typer.echo(json.dumps(facts, indent=2, sort_keys=True))
    else:
        for key, value in facts.items():
            typer.echo(f"{key}: {value}")
        if not facts["enrolled"] and problem is None:
            typer.echo("not joined to a fabric yet: meshforge-agent join")
    if problem is not None:
        raise typer.Exit(ExitCode.CONFIG_ERROR)


@app.command()
def join(
    code: Annotated[
        str | None,
        typer.Argument(
            metavar="[CODE]",
            help="The join code from `fabric invite`. Omit it (or pass -) to type "
            "it at a hidden prompt / read it from stdin - keeps it out of shell "
            "history and the process list.",
        ),
    ] = None,
    config: ConfigOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Replace this laptop's existing enrolment (re-join)."
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1.0, max=300.0, help="Seconds per step."),
    ] = 15.0,
) -> None:
    """Join this laptop to a fabric with a single-use code (V0.7, D136).

    Checks the controller's CA against the code BEFORE sending the code's
    secret, then gets a node certificate for this laptop's key. Until V0.7 G3
    the agent still connects without it.
    """
    import sys

    from meshforge.agent.enrollment import JoinError
    from meshforge.agent.enrollment import join as run_join
    from meshforge.agent.identity import load_or_create
    from meshforge.common.errors import MeshForgeError

    # Joining needs only the data dir: a laptop that has not been told where the
    # controller is yet (discovery off, no controller_address) may still join.
    settings = _load_settings(config, **_LOCAL_ONLY)
    _quiet_logging(settings)
    if code is None or code == "-":
        if sys.stdin.isatty():
            code = typer.prompt("join code", hide_input=True)
        else:
            code = sys.stdin.readline(4096)
    try:
        identity = load_or_create(settings.identity_dir)
        result = run_join(identity, code, force=force, timeout_s=timeout)
    except JoinError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code_for) from None
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    enrollment = result.enrollment
    typer.echo(f"joined fabric {enrollment.fabric_id} via {result.address}")
    typer.echo(f"node_id: {enrollment.node_id}")
    typer.echo(f"certificate valid until {enrollment.not_after.isoformat()}")
    typer.echo(
        "the running agent renews it on its own at 2/3 of its life; start (or "
        "restart) `meshforge-agent run` to connect with it"
    )


@app.command("rotate-key")
def rotate_key(
    config: ConfigOption = None,
    cancel: Annotated[
        bool, typer.Option("--cancel", help="Drop a staged key that is not in use yet.")
    ] = False,
) -> None:
    """Give this laptop a new key, keeping its node_id (V0.7 G4, D140).

    Stages a new P-256 key; the running agent renews its certificate with it
    at its next check (`[agent.cert] renew_check_interval_s`), or right after
    the next start, then switches key, certificate and session together. The
    old key keeps working until then.
    """
    from meshforge.agent.enrollment import JoinError, is_enrolled, load
    from meshforge.agent.identity import (
        discard_rotation_key,
        load_or_create,
        stage_rotation_key,
    )
    from meshforge.common.errors import MeshForgeError

    settings = _load_settings(config, **_LOCAL_ONLY)
    _quiet_logging(settings)
    try:
        identity = load_or_create(settings.identity_dir)
        if cancel:
            dropped = discard_rotation_key(identity.path)
            typer.echo(
                "staged key dropped; the current key stays"
                if dropped
                else "no key was staged"
            )
            return
        if not is_enrolled(identity.path) or load(identity) is None:
            typer.echo(
                f"{PROG_NAME}: this laptop is not enrolled; a new key only "
                "matters with a certificate (meshforge-agent join)",
                err=True,
            )
            raise typer.Exit(ExitCode.CONFIG_ERROR)
        created, fingerprint = stage_rotation_key(identity.path)
    except JoinError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code_for) from None
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    typer.echo(
        ("new key staged" if created else "a new key was already staged")
        + f" (fingerprint {fingerprint[:16]}...) for {identity.node_id}"
    )
    typer.echo(
        "the running agent renews its certificate with it within "
        f"{settings.agent.cert.renew_check_interval_s:.0f} s (or right after its "
        "next start); `meshforge-agent identity show` says when it is done"
    )


@identity_app.command("reset")
def identity_reset(
    config: ConfigOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm: the node gets a NEW node_id; the controller forgets the old one.",
        ),
    ] = False,
) -> None:
    """Delete the identity files; the next `run` mints a fresh node_id."""
    from meshforge.agent.identity import reset
    from meshforge.common.errors import MeshForgeError

    settings = _load_settings(config, **_LOCAL_ONLY)
    _quiet_logging(settings)
    if not yes:
        typer.echo(
            f"{PROG_NAME}: refusing to reset {settings.identity_dir} without --yes "
            "(this node would get a new node_id)",
            err=True,
        )
        raise typer.Exit(ExitCode.USAGE_ERROR)
    try:
        reset(settings.identity_dir)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    typer.echo(f"identity reset: {settings.identity_dir}")


@app.command("migrate-identity")
def migrate_identity(
    from_dir: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="The per-user identity folder to move (default: $SUDO_USER's, "
            "else the only one under /home). Use it when several were found.",
            file_okay=False,
        ),
    ] = None,
    config_from: Annotated[
        Path | None,
        typer.Option(
            "--config-from",
            help="The config.toml to move (default: the same user's).",
            dir_okay=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; write nothing.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root-dir", hidden=True, help="Tests: prefix for / paths."),
    ] = None,
    service_user: Annotated[
        str,
        typer.Option("--service-user", hidden=True, help="Tests: '' = no chown."),
    ] = "meshforge",
) -> None:
    """Move this laptop's per-user identity + config to the system service
    (V0.17 G1, D387) - the node keeps its node_id. Run as root; the package
    runs it on install. Refuses (exit 4, nothing changed) when the choice is
    not clear, and says what to do."""
    import os
    import sys

    from meshforge.agent.migration import Paths, migrate
    from meshforge.common.config import CONFIG_FILE_NAME
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    configure_logging("WARNING", fmt="console", component="agent")
    if sys.platform != "linux":
        typer.echo(
            f"{PROG_NAME}: migrate-identity is Linux-only for now (the Windows "
            "installer migrates on its own)",
            err=True,
        )
        raise typer.Exit(ExitCode.NOT_IMPLEMENTED)
    if not dry_run and service_user and os.geteuid() != 0:
        typer.echo(
            f"{PROG_NAME}: migrate-identity changes machine-wide files: run it with "
            "sudo (or add --dry-run to only see the plan)",
            err=True,
        )
        raise typer.Exit(ExitCode.FAILURE)
    paths = None
    if root_dir is not None:
        paths = Paths(
            target_identity=root_dir / "var/lib/meshforge/identity",
            target_config=root_dir / "etc/meshforge" / CONFIG_FILE_NAME,
            home_root=root_dir / "home",
        )
    try:
        report = migrate(
            explicit=from_dir,
            config_from=config_from,
            dry_run=dry_run,
            paths=paths,
            service_user=service_user or None,
            sudo_user=os.environ.get("SUDO_USER"),
        )
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    except OSError as exc:
        typer.echo(
            f"{PROG_NAME}: migration failed on {exc.filename or 'a file'}: "
            f"{exc.strerror or exc}; nothing was committed - re-run it",
            err=True,
        )
        raise typer.Exit(ExitCode.FAILURE) from None
    if as_json:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        for line in report.lines():
            typer.echo(line)
    raise typer.Exit(report.exit_code)


def _discover_facts(settings: AgentSettings) -> HardwareFacts:
    """One bounded discovery pass (the agent's own start-up path); exits on failure."""
    import asyncio

    from meshforge.agent.hardware import HardwareProvider
    from meshforge.agent.hardware.deadline import BlockingCallError, DeadlineThread
    from meshforge.agent.runtime import DISCOVERY_GRACE_S

    timeout_s = settings.agent.hardware.timeout_s

    async def probe_once() -> HardwareFacts:
        # The same bounded path the agent's start-up uses: a wedged NVML call
        # cannot be cancelled, so it is abandoned on its own daemon thread rather
        # than hanging this command (agent/hardware/deadline.py).
        guard = DeadlineThread("hardware-show")
        provider = HardwareProvider(timeout_s=timeout_s)
        return await guard.call(provider.get, timeout_s=timeout_s + DISCOVERY_GRACE_S)

    try:
        return asyncio.run(probe_once())
    except BlockingCallError as exc:
        typer.echo(
            f"{PROG_NAME}: hardware discovery did not finish within "
            f"{timeout_s + DISCOVERY_GRACE_S:.3g} s ({exc}); raise "
            "[agent.hardware] timeout_s or check the GPU driver",
            err=True,
        )
        raise typer.Exit(ExitCode.FAILURE) from None
    except ImportError as exc:
        typer.echo(
            f"{PROG_NAME}: hardware discovery needs the agent extra "
            f"(`uv sync --extra agent`): {exc}",
            err=True,
        )
        raise typer.Exit(ExitCode.CONFIG_ERROR) from None


@hardware_app.command("show")
def hardware_show(
    config: ConfigOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Probe this machine and print what discovery found - and what it could not.

    Runs entirely on this laptop: no controller, no network. The output includes
    every probe's status, duration and reason, which is exactly the part the
    controller never receives (D13), and the cached hardware scores, if any.
    """
    from meshforge.agent.benchmarks.cache import load_cache
    from meshforge.agent.benchmarks.service import current_fingerprint
    from meshforge.agent.benchmarks.view import score_lines, scores_payload
    from meshforge.agent.hardware.view import facts_lines, facts_payload

    settings = _load_settings(config)
    _quiet_logging(settings)
    facts = _discover_facts(settings)
    fp, _ = current_fingerprint(facts)
    cache = load_cache(settings.data_dir, fp)
    source = (
        f"cached ({cache.status.value}{': ' + cache.reason if cache.reason else ''})"
    )

    if as_json:
        payload = facts_payload(facts)
        payload["scores"] = scores_payload(cache.scores, source="cache", cache=cache)
        typer.echo(json.dumps(payload, sort_keys=True))
        return
    for line in facts_lines(facts):
        typer.echo(line)
    typer.echo("")
    if cache.scores is None:
        typer.echo(
            f"scores: none cached ({cache.status.value}); run "
            f"`{PROG_NAME} hardware score`"
        )
    else:
        for line in score_lines(cache.scores, source=source):
            typer.echo(line)


@hardware_app.command("score")
def hardware_score(
    config: ConfigOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Measure even if a valid cached run exists."),
    ] = False,
) -> None:
    """Measure this machine's hardware scores (CPU, memory, GPU) and cache them.

    Local only, like `hardware show`. Uses the cached run when it is still valid for
    this hardware and software (unless --force). Measurements run in a child process
    at lowered priority within [agent.scores] budget_s. A metric that cannot be
    measured here (no BLAS, no torch, no CUDA) is reported as unavailable with its
    reason - that is a truthful result, exit 0. Exit 7 (BUSY) if another scoring
    run holds the lock.
    """
    import asyncio

    from meshforge.agent.benchmarks import service
    from meshforge.agent.benchmarks.cache import ScorerBusyError, load_cache
    from meshforge.agent.benchmarks.view import score_lines, scores_payload

    settings = _load_settings(config)
    _quiet_logging(settings)
    facts = _discover_facts(settings)
    fp, _ = service.current_fingerprint(facts)
    cache = load_cache(settings.data_dir, fp)
    if cache.valid and cache.scores is not None and not force:
        _print_scores(
            as_json,
            scores_payload(cache.scores, source="cache", cache=cache),
            score_lines(
                cache.scores, source="cached (valid; use --force to re-measure)"
            ),
        )
        return

    scores_cfg = settings.agent.scores

    def announce() -> None:  # called once the lock is held
        if not as_json:
            typer.echo(
                f"measuring (up to {scores_cfg.budget_s:g} s; Ctrl-C stops it) ...",
                err=True,
            )

    exit_code = ExitCode.OK
    try:
        scores = asyncio.run(
            service.score_and_cache(
                settings.data_dir,
                facts,
                service.runner_config(scores_cfg),
                on_start=announce,
            )
        )
    except ScorerBusyError:
        typer.echo(
            f"{PROG_NAME}: a hardware-score run is already in progress (another "
            "`hardware score`, or the agent's background run); try again when it "
            "finishes",
            err=True,
        )
        raise typer.Exit(ExitCode.BUSY) from None
    except service.CacheWriteError as exc:
        typer.echo(f"{PROG_NAME}: measured, but {exc}", err=True)
        scores = exc.scores
        exit_code = ExitCode.FAILURE
    except OSError as exc:
        typer.echo(
            f"{PROG_NAME}: cannot use the data directory: "
            f"{exc.strerror or type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(ExitCode.FAILURE) from None
    except KeyboardInterrupt:
        typer.echo(
            f"{PROG_NAME}: interrupted; the benchmark child was stopped", err=True
        )
        raise typer.Exit(ExitCode.FAILURE) from None
    _print_scores(
        as_json,
        scores_payload(scores, source="measured", cache=None),
        score_lines(scores, source="measured"),
    )
    raise typer.Exit(exit_code)


@app.command()
def prepare(
    config: ConfigOption = None,
    base: Annotated[
        list[str] | None,
        typer.Option(
            "--base",
            help="Base to pre-pull: python-cpu or cuda-pytorch (repeatable; "
            "default: both).",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            min=1.0,
            help="Seconds per image pull (default: [agent.execution] env_timeout_s).",
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Pre-pull the pinned base images so the first job does not wait for them.

    Local only: no controller. Images already present (by digest) are not pulled
    again. A laptop without Docker reports it and pulls nothing (exit 0: the venv
    runtime needs no image). Exit 1 if a pull failed.
    """
    import asyncio

    from meshforge.agent.envs.prepare import BASES
    from meshforge.agent.envs.prepare import prepare as run_prepare

    settings = _load_settings(config)
    _quiet_logging(settings)
    chosen = tuple(base) if base else BASES
    unknown = [name for name in chosen if name not in BASES]
    if unknown:
        typer.echo(
            f"{PROG_NAME}: unknown base {', '.join(unknown)!s}; choose from "
            f"{', '.join(BASES)}",
            err=True,
        )
        raise typer.Exit(ExitCode.USAGE_ERROR)
    if not as_json:
        typer.echo(f"preparing {', '.join(chosen)} (Ctrl-C stops it) ...", err=True)
    try:
        report = asyncio.run(run_prepare(settings, bases=chosen, timeout_s=timeout))
    except KeyboardInterrupt:
        typer.echo(f"{PROG_NAME}: interrupted; the pull was stopped", err=True)
        raise typer.Exit(ExitCode.FAILURE) from None
    if as_json:
        typer.echo(json.dumps(report.as_dict(), sort_keys=True))
    else:
        for line in report.lines():
            typer.echo(line)
    raise typer.Exit(ExitCode.OK if report.ok else ExitCode.FAILURE)


def _print_scores(as_json: bool, payload: dict[str, object], lines: list[str]) -> None:
    if as_json:
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        for line in lines:
            typer.echo(line)


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
