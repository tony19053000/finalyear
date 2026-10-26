"""V0.17 G1: the .deb builder's pure parts and the packaging files (brief §5a items 4-5).

* ``debian_version``: dpkg orders the versions exactly as PEP 440 does;
* ``render_config``: placeholders filled as quoted YAML values, unknown ones
  refused; the real ``packaging/linux/nfpm.yaml`` renders completely;
* staging (fixed modes + mtimes, CRLF refused), the reproducible changelog;
* the ``.deb`` inspector on a hand-made package: missing paths are named;
* the unit / path unit / maintainer scripts say what the brief requires, and
  agree with the code that relies on them (paths, exit codes, user names).

The real package is built and installed in tests/component/packaging (slow).
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import itertools
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Literal

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PACKAGING = REPO / "packaging" / "linux"
EPOCH = 1_700_000_000


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bd = _load("build_deb")
# the bundle_lib build_deb itself imported (one BundleError class for both)
lib = sys.modules[bd.BundleError.__module__]

pytestmark = pytest.mark.unit


_HARDENING = (
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "RestrictSUIDSGID",
    "LockPersonality",
    "RestrictRealtime",
)


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize(
    ("pep440", "debian"),
    [
        ("0.1.0", "0.1.0-1"),
        ("0.1.0.dev0", "0.1.0~~dev0-1"),
        ("0.17.0a1", "0.17.0~a1-1"),
        ("0.17.0b2", "0.17.0~b2-1"),
        ("0.17.0rc1", "0.17.0~rc1-1"),
        ("0.17.0rc1.dev3", "0.17.0~rc1~dev3-1"),
        ("0.17.0.post1", "0.17.0+post1-1"),
        ("0.17.0.post1.dev2", "0.17.0+post1~dev2-1"),
        ("1.2.3.4", "1.2.3.4-1"),
    ],
)
def test_debian_version_mapping(pep440: str, debian: str) -> None:
    assert bd.debian_version(pep440) == debian


def test_debian_version_revision() -> None:
    assert bd.debian_version("0.1.0", 3) == "0.1.0-3"
    with pytest.raises(lib.BundleError):
        bd.debian_version("0.1.0", 0)


@pytest.mark.parametrize("bad", ["1!0.1.0", "0.1.0+local", "v0.1", "0.1.0-1", ""])
def test_debian_version_refuses_epochs_and_local_versions(bad: str) -> None:
    with pytest.raises(lib.BundleError) as caught:
        bd.debian_version(bad)
    assert caught.value.reason == "bad_version"


ORDERED = [
    "0.17.0.dev0",
    "0.17.0a1.dev1",
    "0.17.0a1",
    "0.17.0b1",
    "0.17.0rc1.dev1",
    "0.17.0rc1",
    "0.17.0",
    "0.17.0.post1.dev1",
    "0.17.0.post1",
    "0.17.1.dev0",
    "0.18.0",
]


def test_pep440_order_is_preserved_by_dpkg() -> None:
    version = pytest.importorskip("packaging.version")
    Version = version.Version

    assert sorted(ORDERED, key=Version) == ORDERED  # the list itself is right
    dpkg = shutil.which("dpkg")
    if dpkg is None:
        pytest.skip("dpkg is not installed (the mapping is still pinned above)")
    for low, high in itertools.pairwise(ORDERED):
        a, b = bd.debian_version(low), bd.debian_version(high)
        done = subprocess.run([dpkg, "--compare-versions", a, "lt", b], check=False)
        assert done.returncode == 0, f"dpkg: {a} is not < {b}"


# ------------------------------------------------------------- the config
def test_render_quotes_values_and_leaves_comments() -> None:
    template = (
        "# a ${MF_UNKNOWN} in a comment stays\n"
        "version: ${MF_DEB_VERSION}\n"
        "  - src: ${MF_BUNDLE}\n"
        "    dst: /opt/meshforge/${MF_VERSION}\n"
        "plain: value\n"
    )
    out = bd.render_config(
        template,
        {
            "MF_DEB_VERSION": "0.1.0~~dev0-1",
            "MF_BUNDLE": '/home/a b/#dist/"x"',
            "MF_VERSION": "0.1.0.dev0",
        },
    )
    assert out.splitlines() == [
        "# a ${MF_UNKNOWN} in a comment stays",
        'version: "0.1.0~~dev0-1"',
        '  - src: "/home/a b/#dist/\\"x\\""',
        '    dst: "/opt/meshforge/0.1.0.dev0"',
        "plain: value",
    ]


def test_render_refuses_unknown_and_misplaced_placeholders() -> None:
    with pytest.raises(lib.BundleError, match="unknown placeholder"):
        bd.render_config("a: ${MF_NOPE}\n", {})
    with pytest.raises(lib.BundleError, match="outside"):
        bd.render_config("- ${MF_VERSION}\n", {"MF_VERSION": "1"})
    with pytest.raises(lib.BundleError, match="contains"):
        bd.render_config("a: ${MF_X}\n", {"MF_X": "${MF_Y}"})


def _values() -> dict[str, str]:
    return {
        "MF_DEB_VERSION": "0.1.0~~dev0-1",
        "MF_VERSION": "0.1.0.dev0",
        "MF_BUNDLE": "/b/meshforge-0.1.0.dev0-linux-x86_64",
        "MF_STAGE": "/s",
        "MF_MTIME": "2023-11-14T22:13:20Z",
    }


def test_real_nfpm_config_renders_completely() -> None:
    yaml = pytest.importorskip("yaml")
    rendered = bd.render_config((PACKAGING / "nfpm.yaml").read_text("utf-8"), _values())
    body = "\n".join(
        line for line in rendered.splitlines() if not line.lstrip().startswith("#")
    )
    assert "${" not in body
    doc = yaml.safe_load(rendered)
    assert doc["name"] == bd.PACKAGE and doc["arch"] == bd.DEB_ARCH
    assert doc["version"] == "0.1.0~~dev0-1" and doc["mtime"] == "2023-11-14T22:13:20Z"
    dsts = {c["dst"]: c for c in doc["contents"]}
    assert dsts["/opt/meshforge/0.1.0.dev0"]["type"] == "tree"
    assert dsts["/opt/meshforge/current"] == {
        "src": "0.1.0.dev0",
        "dst": "/opt/meshforge/current",
        "type": "symlink",
    }
    for wrapper in ("meshforge-agent", "compute", "meshforge-console-open"):
        assert (
            dsts[f"/usr/bin/{wrapper}"]["src"]
            == f"/opt/meshforge/current/bin/{wrapper}"
        )
    # V0.17 G3: the service always runs (local console); no path unit any more
    unit = "meshforge-agent.service"
    assert dsts[f"/usr/lib/systemd/system/{unit}"]["src"] == f"/s/{unit}"
    assert "/usr/lib/systemd/system/meshforge-agent.path" not in dsts
    assert dsts["/usr/lib/tmpfiles.d/meshforge-agent.conf"]["src"] == (
        "/s/meshforge-agent.tmpfiles"
    )
    assert dsts["/usr/lib/meshforge/docker-access"]["file_info"]["mode"] == 0o755
    # every staged file is packaged, every packaged stage file is staged
    staged_srcs = {f"/s/{rel}" for rel in bd.STAGED if not rel.startswith("scripts/")}
    packaged = {c["src"] for c in doc["contents"] if str(c["src"]).startswith("/s/")}
    assert packaged - {"/s/changelog.Debian.gz"} == staged_srcs
    assert set(doc["scripts"].values()) == {
        f"/s/{rel}" for rel in bd.STAGED if rel.startswith("scripts/")
    }
    assert "libc6 (>= 2.28)" in doc["depends"]
    assert not any("python" in d for d in doc["depends"])  # the bundle brings its own


# ----------------------------------------------------------------- staging
def test_changelog_is_reproducible_and_debian_shaped() -> None:
    one = bd.changelog_gz("0.1.0-1", "0.1.0", "a" * 40, EPOCH)
    assert one == bd.changelog_gz("0.1.0-1", "0.1.0", "a" * 40, EPOCH)
    text = gzip.decompress(one).decode()
    assert text.startswith("meshforge-agent (0.1.0-1) unstable; urgency=medium\n")
    assert "git aaaaaaaaaaaa" in text
    assert text.rstrip().endswith("Tue, 14 Nov 2023 22:13:20 +0000")


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_stage_fixes_modes_and_mtimes(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    values = {"MF_VERSION": "0.1.0"}
    bd.stage(stage, PACKAGING, b"log", EPOCH, bd.STAGED, values)
    for rel, mode in bd.STAGED.items():
        st = (stage / rel).stat()
        assert st.st_mode & 0o777 == mode, rel
        assert int(st.st_mtime) == EPOCH, rel
        source = (PACKAGING / rel).read_bytes()
        if rel.startswith(("scripts/", "upgrade-recover")):  # V0.17 G5: inlined
            source = bd.expand_script(source.decode(), values).encode()
        assert (stage / rel).read_bytes() == source
    assert (stage / "changelog.Debian.gz").read_bytes() == b"log"


def test_stage_refuses_crlf_and_missing_files(tmp_path: Path) -> None:
    packaging = tmp_path / "packaging"
    shutil.copytree(PACKAGING, packaging)
    (packaging / "scripts" / "postinst").write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    (tmp_path / "s1").mkdir()
    with pytest.raises(lib.BundleError, match="CRLF"):
        bd.stage(tmp_path / "s1", packaging, b"", EPOCH, bd.STAGED, {"MF_VERSION": "1"})
    (packaging / "scripts" / "postinst").unlink()
    (tmp_path / "s2").mkdir()
    with pytest.raises(lib.BundleError, match="missing packaging file"):
        bd.stage(tmp_path / "s2", packaging, b"", EPOCH, bd.STAGED, {"MF_VERSION": "1"})


# ---------------------------------------------------------------- inspection
def _tar(
    entries: list[tarfile.TarInfo],
    payload: dict[str, bytes],
    mode: Literal["w:gz", "w:xz"],
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for info in entries:
            data = payload.get(info.name)
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buffer.getvalue()


def _file(name: str) -> tarfile.TarInfo:
    return tarfile.TarInfo(name)


def _link(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}".encode()
            + b"`\n"
        )
        out += header + data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def _deb(tmp_path: Path, *, drop: str = "", current: str = "1.0") -> Path:
    control = b"Package: meshforge-agent\nVersion: 1.0-1\nArchitecture: amd64\n"
    control_tar = _tar(
        [_file(f"./{n}") for n in bd.REQUIRED_CONTROL],
        {"./control": control},
        "w:gz",
    )
    names = [t.format(v="1.0") for t, kind in bd.REQUIRED_PATHS if kind == "file"]
    links = [t.format(v="1.0") for t, kind in bd.REQUIRED_PATHS if kind == "symlink"]
    entries = [_file(n) for n in names if n != drop]
    entries += [
        _link(n, current if n.endswith("/current") else "/x")
        for n in links
        if n != drop
    ]
    data_tar = _tar(entries, dict.fromkeys(names, b"x"), "w:xz")
    path = tmp_path / "t.deb"
    path.write_bytes(
        _ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", control_tar),
                ("data.tar.xz", data_tar),
            ]
        )
    )
    return path


def test_inspector_accepts_a_complete_package(tmp_path: Path) -> None:
    contents = bd.inspect_deb(_deb(tmp_path))
    assert contents.members == ("debian-binary", "control.tar.gz", "data.tar.xz")
    assert bd.check_contents(contents, "1.0", "1.0-1") == []


def test_inspector_names_what_is_missing_or_wrong(tmp_path: Path) -> None:
    missing = "./usr/share/applications/meshforge.desktop"
    problems = bd.check_contents(
        bd.inspect_deb(_deb(tmp_path, drop=missing)), "1.0", "1.0-1"
    )
    assert problems == [f"missing {missing}"]
    problems = bd.check_contents(
        bd.inspect_deb(_deb(tmp_path, current="0.9")), "1.0", "1.0-1"
    )
    assert problems == ["/opt/meshforge/current -> 0.9 (want 1.0)"]
    problems = bd.check_contents(bd.inspect_deb(_deb(tmp_path)), "1.0", "1.0-2")
    assert problems == ["control Version: '1.0-1' (want '1.0-2')"]


def test_inspector_refuses_non_debs(tmp_path: Path) -> None:
    bad = tmp_path / "x.deb"
    bad.write_bytes(b"PK\x03\x04 not an ar archive")
    with pytest.raises(lib.BundleError, match="not an ar archive"):
        bd.inspect_deb(bad)
    bad.write_bytes(_ar([("debian-binary", b"2.0\n")]))
    with pytest.raises(lib.BundleError, match="unexpected"):
        bd.inspect_deb(bad)


# -------------------------------------------------------------- the bundle
def _mini_bundle(root: Path, **meta: object) -> Path:
    root.mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "meshforge-agent").write_text("#!/bin/sh\n")
    info = {
        "role": "agent",
        "os": "linux",
        "arch": "x86_64",
        "version": "1.0",
        "source_date_epoch": EPOCH,
        "git_sha": "a" * 40,
        **meta,
    }
    manifest = lib.Manifest(meta=info)
    (root / lib.BUILDINFO_NAME).write_text(manifest.buildinfo_json())
    files = lib.scan_tree(root)
    (root / lib.MANIFEST_NAME).write_text(lib.manifest_text(files))
    return root


def test_bundle_facts_refuse_the_wrong_bundle(tmp_path: Path) -> None:
    assert bd.bundle_facts(_mini_bundle(tmp_path / "ok"))["version"] == "1.0"
    for name, meta in (
        ("controller", {"role": "controller"}),
        ("windows", {"os": "windows"}),
    ):
        with pytest.raises(lib.BundleError) as caught:
            bd.bundle_facts(_mini_bundle(tmp_path / name, **meta))
        assert caught.value.reason == "wrong_bundle"
    with pytest.raises(lib.BundleError) as caught:
        bd.bundle_facts(tmp_path / "nothing")
    assert caught.value.reason == "no_bundle"


def test_build_refuses_a_modified_bundle_before_any_download(tmp_path: Path) -> None:
    bundle = _mini_bundle(tmp_path / "b")
    (bundle / "bin" / "meshforge-agent").write_text("#!/bin/sh\necho changed\n")
    options = bd.DebOptions(
        bundle=bundle, out=tmp_path / "out", cache=tmp_path / "cache"
    )
    with pytest.raises(lib.BundleError) as caught:
        bd.build_deb(options)
    assert caught.value.reason == "bundle_modified"
    assert not (tmp_path / "cache").exists()


# ------------------------------------------------------- the packaging files
def _unit(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in (PACKAGING / name).read_text("utf-8").splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


def test_service_unit_is_the_hardened_system_service() -> None:
    from meshforge.agent.execution.cpu_cgroup import LEAF_NAME
    from meshforge.agent.identity import NODE_CERT_FILE_NAME
    from meshforge.agent.settings import SYSTEM_CONSOLE_TOKEN_DIR
    from meshforge.agent.system_cli import (
        INSTALL_MODE_ENV_VAR,
        SERVICE_USER,
        SYSTEM_LAUNCHER,
        SYSTEM_LINUX_DATA_DIR,
    )
    from meshforge.common.exit_codes import ExitCode

    unit = _unit("meshforge-agent.service")
    assert unit["User"] == [SERVICE_USER] and unit["Group"] == [SERVICE_USER]
    assert unit["ExecStart"] == [f"{SYSTEM_LAUNCHER} run"]
    assert unit["Environment"] == [f"{INSTALL_MODE_ENV_VAR}=system"]
    assert unit["Restart"] == ["always"]
    assert set(unit["RestartPreventExitStatus"][0].split()) == {
        str(int(ExitCode.CONFIG_ERROR)),
        str(int(ExitCode.CONFLICT)),
    }
    assert unit["ProtectSystem"] == ["strict"]
    # V0.17 G3: + the console token folder ("-": optional)
    assert unit["ReadWritePaths"] == [
        f"{SYSTEM_LINUX_DATA_DIR} -{SYSTEM_CONSOLE_TOKEN_DIR}"
    ]
    for key in ("ProtectHome", "PrivateTmp", "NoNewPrivileges", "Delegate"):
        assert unit[key] == ["yes"], key
    for key in _HARDENING:
        assert unit[key] == ["yes"], key
    assert unit["UMask"] == ["0077"]
    # Measured to break GPU jobs on systemd 249 / jobs' own namespaces (unit comment).
    assert "ProtectClock" not in unit and "RestrictNamespaces" not in unit
    assert unit["DelegateSubgroup"] == [LEAF_NAME]
    assert unit["KillMode"] == ["mixed"]
    assert unit["WantedBy"] == ["multi-user.target"]
    enrolment = f"{SYSTEM_LINUX_DATA_DIR}/identity/{NODE_CERT_FILE_NAME}"
    # V0.17 G3: the service runs before the join (its console is how one joins)
    assert "ConditionPathExists" not in unit
    postinst = (PACKAGING / "scripts" / "postinst").read_text("utf-8")
    assert f"ENROLMENT={enrolment}\n" in postinst
    assert "docker" not in " ".join(unit.get("SupplementaryGroups", []))  # Q12


MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm")


@pytest.mark.parametrize("name", MAINTAINER_SCRIPTS)
def test_maintainer_scripts_are_strict_posix_sh(name: str) -> None:
    path = PACKAGING / "scripts" / name
    data = path.read_bytes()
    assert b"\r" not in data
    text = data.decode("utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\nset -eu\n" in text
    assert "\nexit 0\n" in text
    sh = shutil.which("sh")
    if sh is not None:
        syntax = subprocess.run([sh, "-n", str(path)], capture_output=True, check=False)
        assert syntax.returncode == 0, syntax.stderr
    checker = shutil.which("shellcheck")
    if checker is not None:
        done = subprocess.run(
            [checker, "-s", "sh", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout


def test_maintainer_scripts_keep_data_on_remove_and_never_add_docker() -> None:
    scripts = {
        n: (PACKAGING / "scripts" / n).read_text("utf-8") for n in MAINTAINER_SCRIPTS
    }
    postrm = scripts["postrm"]
    # V0.17 G2: the purge branch has an inner `case`, so it ends at `remove)`
    purge = postrm[postrm.index("purge)") : postrm.index("\nremove)")]
    assert "rm -rf /var/lib/meshforge /etc/meshforge" in purge
    # V0.17 G2: with meshforge-controller on the machine its state is kept
    assert "controller_known" in purge and "controller | db" in purge
    remove = postrm[postrm.index("\nremove)") :]
    assert "rm -rf" not in remove[: remove.index(";;")]
    for name, text in scripts.items():  # Q12: docker access is printed, never granted
        for line in text.splitlines():
            if "usermod" in line and "docker" in line:
                assert line.strip().startswith("say "), (name, line)
    # prerm stops on remove only, never on upgrade
    prerm = scripts["prerm"]
    upgrade = prerm[prerm.index("upgrade | failed-upgrade)") :]
    assert "systemctl" not in upgrade[: upgrade.index(";;")]
    assert "migrate-identity" in scripts["postinst"]


# ------------------------------------------------------- system libraries
LDD = """\
/b/python/bin/python3.12:
\tlinux-vdso.so.1 (0x00007ffd)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f00)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f01)
/b/python/lib/x.so:
\tlibgfortran-1.so.5 => /b/python/lib/../libs/libgfortran-1.so.5 (0x1)
\tlibquadmath-2.so.0 => /b/python/lib/a dir with spaces/libquadmath-2.so.0 (0x7f3e)
\tlibz.so.1 => /lib/x86_64-linux-gnu/libz.so.1 (0x2)
\tlibfoo.so.3 => /usr/lib/libfoo.so.3 (0x3)
\tlibbar.so.1 => not found
/b/python/bin/uv:
\tstatically linked
"""


def test_classify_ldd(tmp_path: Path) -> None:
    packages, problems = bd.classify_ldd(LDD, Path("/b"))
    assert packages == {"libc6", "zlib1g"}
    assert problems == [
        "/b/python/lib/x.so: needs libfoo.so.3 from the system (/usr/lib/libfoo.so.3)",
        "/b/python/lib/x.so: libbar.so.1 not found",
    ]


def test_every_known_system_library_is_declared() -> None:
    depends = bd.nfpm_depends((PACKAGING / "nfpm.yaml").read_text("utf-8"))
    names = {d.split(" ", 1)[0] for d in depends}
    assert set(bd.SYSTEM_LIBS.values()) <= names
    assert "passwd" in names


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("ldd") is None, reason="ldd"
)
def test_check_shared_libs_on_a_real_elf(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    real = Path(sys.executable).resolve()
    shutil.copy2(real, bundle / "python")
    assert bd.elf_files(bundle) == [bundle / "python"]
    assert (
        bd.check_shared_libs(bundle, ["libc6", *sorted(set(bd.SYSTEM_LIBS.values()))])
        == []
    )
    problems = bd.check_shared_libs(bundle, [])
    assert problems and all("nfpm.yaml does not depend" in p for p in problems)
