"""The meshforge-agent .deb under test, and clean Ubuntu systemd containers (V0.17 G1, Q16).

* :func:`built_debs` - the package in two Debian revisions (``-1`` and ``-2``, the
  same bundle) for install and upgrade. CI builds them once in the
  ``package-linux`` job and points ``MESHFORGE_TEST_DEB_DIR`` at them; locally they
  are built here (``scripts/build_bundle.py`` + ``scripts/build_deb.py``, minutes
  on a cold cache - the tests using this are ``slow``).
* :class:`SystemdBox` - ``ubuntu:<22.04|24.04>`` with systemd as PID 1
  (``packaging/linux/testing/systemd.Dockerfile``), privileged with a private
  cgroup namespace so systemd can delegate cgroups as on a laptop. Every command
  goes through ``docker exec``; containers carry the MeshForge test labels, so
  the stale-container sweep also reaps one a killed test left behind.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mf_test_containers import meshforge_test_labels

REPO: Final = Path(__file__).resolve().parents[2]
DOCKERFILE: Final = REPO / "packaging" / "linux" / "testing" / "systemd.Dockerfile"
DEB_DIR_ENV: Final = "MESHFORGE_TEST_DEB_DIR"
UBUNTU_VERSIONS: Final = ("22.04", "24.04")
UNIT: Final = "meshforge-agent.service"
PATH_UNIT: Final = "meshforge-agent.path"
"""Shipped by G1 only; V0.17 G3 removed it (the service always runs)."""


@dataclass(frozen=True)
class Debs:
    first: Path
    """Debian revision 1."""
    second: Path
    """The same bundle, revision 2: an upgrade."""
    bundle_version: str


def _pick(folder: Path, revision: int) -> Path | None:
    found = sorted(folder.glob(f"meshforge-agent_*-{revision}_amd64.deb"))
    return found[-1] if found else None


def built_debs(work: Path) -> Debs:
    """The two packages: from ``$MESHFORGE_TEST_DEB_DIR`` when set (CI), else
    built into ``work`` (reusing the shared download cache)."""
    preset = os.environ.get(DEB_DIR_ENV)
    if preset:
        folder = Path(preset)
        first, second = _pick(folder, 1), _pick(folder, 2)
        if first is None or second is None:
            raise RuntimeError(f"{DEB_DIR_ENV}={folder} needs a -1 and a -2 .deb")
    else:
        # The component conftest points SSL_CERT_FILE at the test fabric CA;
        # the bundle build downloads from the internet.
        env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
        scripts = REPO / "scripts"
        subprocess.run(
            [sys.executable, str(scripts / "build_bundle.py"), "--role", "agent",
             "--out", str(work), "--force", "--no-archive"],
            check=True, timeout=1500, env=env,
        )  # fmt: skip
        (bundle,) = [
            p for p in work.iterdir() if p.is_dir() and p.name.startswith("meshforge-")
        ]
        for revision in (1, 2):
            subprocess.run(
                [sys.executable, str(scripts / "build_deb.py"), "--bundle", str(bundle),
                 "--out", str(work), "--revision", str(revision), "--force"],
                check=True, timeout=600, env=env,
            )  # fmt: skip
        folder = work
        first, second = _pick(folder, 1), _pick(folder, 2)
        assert first is not None and second is not None
    version = package_bundle_version(first)
    return Debs(first, second, version)


CONTROLLER_DEB_DIR_ENV: Final = "MESHFORGE_TEST_CONTROLLER_DEB_DIR"
CONTROLLER_UNIT: Final = "meshforge-controller.service"


def _pick_controller(folder: Path, revision: int) -> Path | None:
    found = sorted(folder.glob(f"meshforge-controller_*-{revision}_amd64.deb"))
    return found[-1] if found else None


def built_controller_debs(work: Path) -> Debs:
    """The meshforge-controller package in revisions 1 and 2 (V0.17 G2): from
    ``$MESHFORGE_TEST_CONTROLLER_DEB_DIR`` when set (CI), else built here
    (controller bundle with the dashboard and PostgreSQL, then two .debs).
    Built with the Q17 .deb size gate on (build_bundle STRIP_SITE keeps it under)."""
    preset = os.environ.get(CONTROLLER_DEB_DIR_ENV)
    if preset:
        folder = Path(preset)
    else:
        env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
        scripts = REPO / "scripts"
        subprocess.run(
            [sys.executable, str(scripts / "build_bundle.py"), "--role", "controller",
             "--out", str(work), "--force", "--no-archive"],
            check=True, timeout=2400, env=env,
        )  # fmt: skip
        (bundle,) = [
            p for p in work.iterdir() if p.is_dir() and p.name.startswith("meshforge-")
        ]
        for revision in (1, 2):
            subprocess.run(
                [sys.executable, str(scripts / "build_deb.py"), "--role", "controller",
                 "--bundle", str(bundle), "--out", str(work), "--revision",
                 str(revision), "--force"],
                check=True, timeout=900, env=env,
            )  # fmt: skip
        folder = work
    first, second = _pick_controller(folder, 1), _pick_controller(folder, 2)
    if first is None or second is None:
        raise RuntimeError(f"{folder} needs a meshforge-controller -1 and -2 .deb")
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import build_deb

    link = build_deb.inspect_deb(first).data.get(
        "./opt/meshforge/controller/current", ""
    )
    return Debs(first, second, link.removeprefix("symlink:"))


def package_bundle_version(deb: Path) -> str:
    """The bundle version inside ``deb`` (``/opt/meshforge/current`` -> it),
    read with ``scripts/build_deb.py``'s own inspector (no dpkg needed)."""
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import build_deb

    target = build_deb.inspect_deb(deb).data.get("./opt/meshforge/current", "")
    if not target.startswith("symlink:"):
        raise RuntimeError(f"{deb.name}: no /opt/meshforge/current symlink")
    return target.removeprefix("symlink:")


def systemd_image(ubuntu: str) -> str:
    """Build (docker caches it) and name the systemd test image."""
    tag = f"meshforge-systemd:{ubuntu}"
    subprocess.run(
        ["docker", "build", "-q", "-f", str(DOCKERFILE), "--build-arg",
         f"UBUNTU={ubuntu}", "-t", tag, str(DOCKERFILE.parent)],
        check=True, capture_output=True, timeout=900,
    )  # fmt: skip
    return tag


class SystemdBox:
    """One running systemd container (see the module docstring)."""

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def start(
        cls, image: str, *, host_network: bool = False, name_hint: str = "deb"
    ) -> SystemdBox:
        name = f"mf-test-{name_hint}-{os.getpid()}-{time.monotonic_ns() % 10**9}"
        argv = ["docker", "run", "-d", "--name", name, "--privileged",
                "--cgroupns=private", "--tmpfs", "/run", "--tmpfs", "/run/lock"]  # fmt: skip
        for key, value in meshforge_test_labels().items():
            argv += ["--label", f"{key}={value}"]
        if host_network:
            argv += ["--network", "host"]
        subprocess.run([*argv, image], check=True, capture_output=True, timeout=120)
        box = cls(name)
        box.wait_for_systemd()
        return box

    def remove(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.name],
            capture_output=True,
            timeout=120,
            check=False,
        )

    # ------------------------------------------------------------- commands
    def run(
        self,
        command: str,
        *,
        user: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 300,
    ) -> subprocess.CompletedProcess[str]:
        """``sh -c command`` in the container (never raises on a non-zero exit)."""
        argv = ["docker", "exec"]
        if stdin is not None:
            argv.append("-i")
        if user is not None:
            argv += ["-u", user]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.name, "sh", "-c", command]
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def ok(self, command: str, **kwargs: object) -> str:
        """``run`` that must succeed; its stdout."""
        done = self.run(command, **kwargs)  # type: ignore[arg-type]
        assert done.returncode == 0, (
            f"{command!r} exited {done.returncode}\n--- stdout\n{done.stdout[-3000:]}"
            f"\n--- stderr\n{done.stderr[-3000:]}"
        )
        return done.stdout

    def copy_in(self, source: Path, target: str) -> None:
        subprocess.run(
            ["docker", "cp", str(source), f"{self.name}:{target}"],
            check=True, capture_output=True, timeout=300,
        )  # fmt: skip

    def apt(self, args: str) -> subprocess.CompletedProcess[str]:
        return self.run(
            f"DEBIAN_FRONTEND=noninteractive apt-get {args} 2>&1", timeout=600
        )

    def install(self, deb: Path) -> subprocess.CompletedProcess[str]:
        """``apt-get install ./<deb>`` (no network needed: the image has the deps)."""
        self.copy_in(deb, f"/tmp/{deb.name}")
        return self.apt(f"install -y /tmp/{deb.name}")

    # -------------------------------------------------------------- systemd
    def wait_for_systemd(self, within: float = 60) -> None:
        deadline = time.monotonic() + within
        state = ""
        while time.monotonic() < deadline:
            state = self.run(
                "systemctl is-system-running 2>/dev/null || true"
            ).stdout.strip()
            if state in ("running", "degraded"):
                return
            time.sleep(0.5)
        raise AssertionError(f"systemd in {self.name} did not come up ({state!r})")

    def show(self, unit: str, *props: str) -> dict[str, str]:
        out = self.ok(f"systemctl show {unit} " + " ".join(f"-p {p}" for p in props))
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def wait_unit(
        self, unit: str, states: Sequence[str], within: float = 60
    ) -> dict[str, str]:
        """Until ``ActiveState`` is one of ``states``; the last ``show``."""
        deadline = time.monotonic() + within
        facts: dict[str, str] = {}
        while time.monotonic() < deadline:
            facts = self.show(unit, "ActiveState", "SubState", "MainPID", "NRestarts",
                              "ExecMainStatus", "Result")  # fmt: skip
            if facts.get("ActiveState") in states:
                return facts
            time.sleep(0.5)
        raise AssertionError(
            f"{unit} not in {states} within {within} s: {facts}\n{self.journal(unit)}"
        )

    def journal(self, unit: str = UNIT, lines: int = 80) -> str:
        return self.run(f"journalctl -u {unit} --no-pager -n {lines}").stdout

    def journal_events(self, unit: str = UNIT) -> list[dict[str, object]]:
        """The agent's structlog JSON lines from the journal."""
        out = self.run(f"journalctl -u {unit} --no-pager -o cat").stdout
        events: list[dict[str, object]] = []
        for line in out.splitlines():
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events

    # --------------------------------------------------------------- agent
    def identity(self) -> dict[str, object]:
        """``meshforge-agent identity show --json`` run by root -> re-executed as
        the service user (Q15)."""
        out = self.ok("meshforge-agent identity show --json")
        return dict(json.loads(out))

    def verify_uninstall(
        self, *, data_removed: bool, package: str = "all", as_json: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """``lab/verify_uninstall.py`` against this container (V0.17 G5:
        ``package`` = the one of two packages removed; ``as_json`` for the
        CI artefact)."""
        argv = [sys.executable, str(REPO / "lab" / "verify_uninstall.py"), "--os", "linux",
                "--docker", self.name, "--package", package]  # fmt: skip
        if data_removed:
            argv.append("--data-removed")
        if as_json:
            argv.append("--json")
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )


def docker_cli_missing() -> str | None:
    return None if shutil.which("docker") else "the docker CLI is not on PATH"
