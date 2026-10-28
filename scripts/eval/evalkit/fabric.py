"""What the campaign runner needs from a fabric, and the real one over REST.

:class:`Fabric` is the runner's whole view of the system: submit a job folder
with a placement, read a job and its decisions, cancel, describe the fabric.
:class:`HttpFabric` implements it with the CLI's own client and upload code
(``meshforge.cli.client`` / ``meshforge.cli.transfer`` - the exact path
``compute run`` takes), so an evaluated job is an ordinary user job.

:class:`ProfileSwitcher` changes the controller's planner profile
(:mod:`.arms`); :class:`CommandSwitcher` runs the operator's hook command
(``--profile-hook``) and then waits until the controller answers and every
node of the starting inventory is ONLINE again.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from evalkit.arms import Profile

if TYPE_CHECKING:
    from meshforge.cli.client import ControllerClient

__all__ = [
    "CommandSwitcher",
    "Fabric",
    "FabricError",
    "HttpFabric",
    "ProfileSwitcher",
    "wait_ready",
]


class FabricError(RuntimeError):
    """The fabric did not do what the runner needs (typed by ``kind``)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class Fabric(Protocol):
    def describe(self) -> dict[str, Any]:
        """Controller version + node inventory (recorded per session)."""
        ...

    def online_nodes(self) -> set[str]: ...

    def submit(
        self,
        folder: Path,
        manifest: Mapping[str, Any],
        placement: Mapping[str, Any],
        *,
        submitter_node_id: str,
        idempotency_key: str,
    ) -> str:
        """Upload ``folder`` and submit it; the job id (an existing job's id
        when the key was used before)."""
        ...

    def job(self, job_id: str) -> Mapping[str, Any]: ...

    def decisions(self, job_id: str) -> Sequence[Mapping[str, Any]]: ...

    def cancel(self, job_id: str) -> None: ...


class ProfileSwitcher(Protocol):
    def apply(self, profile: Profile, toml_path: Path) -> None:
        """Make the controller run with ``profile`` (raise
        :class:`FabricError` if it cannot)."""
        ...


class HttpFabric:
    """:class:`Fabric` over the controller's REST API (``ControllerClient``)."""

    def __init__(
        self,
        client: ControllerClient,
        *,
        seed: Callable[[Any], bool] | None = None,
    ) -> None:
        """``seed``: copies a built tree into this laptop's agent cache
        (``compute run``'s D109 seeding); None = never seed."""
        self._client = client
        self._seed = seed

    def describe(self) -> dict[str, Any]:
        nodes = self._client.nodes()
        details = []
        for item in sorted(nodes.get("nodes", ()), key=lambda n: str(n["node_id"])):
            try:
                details.append(dict(self._client.node(str(item["node_id"]))))
            except Exception as exc:  # noqa: BLE001 - recorded, not fatal
                details.append({"node_id": item["node_id"], "error": repr(exc)})
        version: str | None = None
        try:
            spec = self._client._get("/v1/openapi.json")
            version = str(spec.get("info", {}).get("version"))
        except Exception:  # noqa: BLE001 - optional
            version = None
        return {
            "controller_url": self._client.controller_url,
            "controller_version": version,
            "nodes": details,
        }

    def online_nodes(self) -> set[str]:
        nodes = self._client.nodes()
        return {
            str(n["node_id"])
            for n in nodes.get("nodes", ())
            if n.get("state") == "online"
        }

    def submit(
        self,
        folder: Path,
        manifest: Mapping[str, Any],
        placement: Mapping[str, Any],
        *,
        submitter_node_id: str,
        idempotency_key: str,
    ) -> str:
        from meshforge.cli.transfer import upload_tree
        from meshforge.common.tree import build_tree

        built = build_tree(folder)
        upload_tree(self._client, built)
        body: dict[str, Any] = {
            "manifest": dict(manifest),
            "tree_id": str(built.tree.tree_id),
            "placement": dict(placement),
            "submitter_node_id": submitter_node_id,
        }
        if self._seed is not None:
            body["seeded"] = bool(self._seed(built))
        created = self._client.submit_job(body, idempotency_key=idempotency_key)
        return str(created["job_id"])

    def job(self, job_id: str) -> Mapping[str, Any]:
        return self._client.job(job_id)

    def decisions(self, job_id: str) -> Sequence[Mapping[str, Any]]:
        return list(self._client.decisions(job_id, limit=200).get("decisions", ()))

    def cancel(self, job_id: str) -> None:
        self._client.cancel(job_id)


def wait_ready(
    fabric: Fabric,
    expected_nodes: set[str],
    *,
    timeout_s: float,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until the controller answers and every ``expected_nodes`` is
    ONLINE (after a profile switch restarted the controller)."""
    deadline = clock() + timeout_s
    last = "no answer"
    while True:
        try:
            online = fabric.online_nodes()
            missing = expected_nodes - online
            if not missing:
                return
            last = f"not online yet: {sorted(missing)}"
        except Exception as exc:  # noqa: BLE001 - the controller is restarting
            last = repr(exc)
        if clock() >= deadline:
            raise FabricError("not_ready", f"after {timeout_s:g} s: {last}")
        sleep(poll_s)


class CommandSwitcher:
    """Runs ``template`` (``{profile}``, ``{toml}`` and ``{env}`` - the
    profile's name, its TOML drop-in and its systemd environment file -
    substituted, split with POSIX rules, no shell) and requires exit 0."""

    def __init__(self, template: str, *, timeout_s: float = 600.0) -> None:
        self.template = template
        self.timeout_s = timeout_s

    def apply(self, profile: Profile, toml_path: Path) -> None:
        argv = [
            part.replace("{profile}", profile.name)
            .replace("{toml}", str(toml_path))
            .replace("{env}", str(toml_path.with_suffix(".env")))
            for part in shlex.split(self.template)
        ]
        try:
            done = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FabricError("profile_hook", f"{argv[0]}: {exc!r}") from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
            raise FabricError(
                "profile_hook",
                f"{argv[0]} exited {done.returncode}: " + " | ".join(tail),
            )
