"""Build the ``meshforge-agent`` / ``meshforge-controller`` .deb from a bundle
(V0.17 G1 agent, G2 controller ``--role controller``; D394).

Input: a built **agent** bundle (``scripts/build_bundle.py --role agent``), checked
against its own ``MANIFEST.sha256`` first. Output:
``<out>/meshforge-agent_<debian version>_amd64.deb`` + ``.sha256`` next to it.

Steps:

1. **Bundle facts** from ``BUILDINFO.json``: role ``agent``, ``linux``/``x86_64``,
   its version and ``source_date_epoch``. A bundle that differs from its manifest
   is refused (``bundle_modified``). Then ``ldd`` on every ELF file: each library
   comes from the bundle or is a known system one whose package ``nfpm.yaml``
   depends on (``system_libs``) - the ``Depends:`` line is checked, not guessed.
2. **Debian version** from the PEP 440 version (:func:`debian_version`), so dpkg
   orders versions the way Python does: ``0.1.0.dev0`` -> ``0.1.0~~dev0-1``.
3. **nfpm** - the pinned release in ``packaging/pins.toml [nfpm]`` (URL + sha256,
   never "latest"), downloaded into the cache and checked before use (the same
   ``fetch`` as the bundle builder).
4. **Stage** the packaging files (``packaging/linux``: unit, maintainer scripts,
   copyright, lintian overrides) + a generated ``changelog.Debian.gz``, all with
   fixed modes and mtime = the bundle's ``source_date_epoch``.
5. **Render** ``packaging/linux/nfpm.yaml``: its ``${MF_*}`` placeholders are
   filled here (nfpm does not expand variables in ``contents``), every filled
   value as a quoted YAML string (paths with spaces or ``#`` stay one value); an
   unknown or leftover placeholder fails the build.
6. **Package** with nfpm, then **inspect** the result with no Debian tools (the
   ``ar`` members, ``control``, the data tree): every path the service and the
   wrappers rely on must be in it, and the size within the agent's download
   budget (Q17). Same bundle + same revision -> byte-identical ``.deb``.

``lintian`` and the install tests run on CI / in the component tests
(``tests/component/packaging``), not here: this script only needs Python.

Usage (from the repo)::

    uv run python scripts/build_deb.py --bundle dist/meshforge-<ver>-linux-x86_64 --out dist/
    uv run python scripts/build_deb.py --build-bundle --out dist/   # bundle first

Exit codes: 0 ok; 1 build or check failed; 2 usage error.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SCRIPTS: Final = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:  # loaded by path (tests) rather than run
    sys.path.insert(0, str(_SCRIPTS))

from bundle_lib import (  # noqa: E402
    BUILDINFO_NAME,
    BundleError,
    check_size_budget,
    load_pins,
    pinned,
    sha256_file,
    verify_bundle,
)

REPO: Final = Path(__file__).resolve().parent.parent
PACKAGING: Final = REPO / "packaging" / "linux"
PINS: Final = REPO / "packaging" / "pins.toml"
DEB_ARCH: Final = "amd64"
MAINTAINER: Final = "MeshForge team <meshforge@example.org>"


@dataclass(frozen=True, slots=True)
class PackageSpec:
    """One .deb built from one bundle role (V0.17 G1 agent, G2 controller)."""

    package: str
    role: str
    packaging: Path
    staged: Mapping[str, int]
    """file in ``packaging`` -> mode in the stage (and so in the package)"""
    required_paths: tuple[tuple[str, str], ...]
    """(path in the data tree, kind); ``{v}`` = the bundle version"""
    current_link: str


AGENT: Final = PackageSpec(
    package="meshforge-agent",
    role="agent",
    packaging=PACKAGING,
    staged={
        "meshforge-agent.service": 0o644,
        "meshforge-agent.tmpfiles": 0o644,
        "desktop/meshforge.desktop": 0o644,
        "desktop/meshforge.svg": 0o644,
        "desktop/docker-access": 0o755,
        "desktop/org.meshforge.docker-access.policy": 0o644,
        "lintian-overrides": 0o644,
        "copyright": 0o644,
        "scripts/preinst": 0o755,
        "scripts/postinst": 0o755,
        "scripts/prerm": 0o755,
        "scripts/postrm": 0o755,
    },
    required_paths=(
        ("./opt/meshforge/{v}/bin/meshforge-agent", "file"),
        ("./opt/meshforge/{v}/bin/compute", "file"),
        ("./opt/meshforge/{v}/python/bin/python3.12", "file"),
        ("./opt/meshforge/{v}/BUILDINFO.json", "file"),
        ("./opt/meshforge/{v}/MANIFEST.sha256", "file"),
        ("./opt/meshforge/current", "symlink"),
        ("./usr/bin/meshforge-agent", "symlink"),
        ("./usr/bin/compute", "symlink"),
        ("./usr/lib/systemd/system/meshforge-agent.service", "file"),
        ("./usr/bin/meshforge-console-open", "symlink"),
        ("./opt/meshforge/{v}/bin/meshforge-console-open", "file"),
        ("./usr/share/applications/meshforge.desktop", "file"),
        ("./usr/lib/tmpfiles.d/meshforge-agent.conf", "file"),
        ("./usr/lib/meshforge/docker-access", "file"),
        ("./usr/share/polkit-1/actions/org.meshforge.docker-access.policy", "file"),
        ("./usr/share/doc/meshforge-agent/copyright", "file"),
        ("./usr/share/doc/meshforge-agent/changelog.Debian.gz", "file"),
    ),
    current_link="./opt/meshforge/current",
)

CONTROLLER: Final = PackageSpec(
    package="meshforge-controller",
    role="controller",
    packaging=PACKAGING / "controller",
    staged={
        "meshforge-controller.service": 0o644,
        "meshforge-controller.ufw": 0o644,
        "lintian-overrides": 0o644,
        "copyright": 0o644,
        "scripts/preinst": 0o755,
        "scripts/postinst": 0o755,
        "scripts/prerm": 0o755,
        "scripts/postrm": 0o755,
    },
    required_paths=(
        ("./opt/meshforge/controller/{v}/bin/meshforge-controller", "file"),
        ("./opt/meshforge/controller/{v}/python/bin/python3.12", "file"),
        ("./opt/meshforge/controller/{v}/pgsql/bin/postgres", "file"),
        ("./opt/meshforge/controller/{v}/pgsql/bin/pg_dump", "file"),
        ("./opt/meshforge/controller/{v}/pgsql/LICENSE", "file"),
        ("./opt/meshforge/controller/{v}/BUILDINFO.json", "file"),
        ("./opt/meshforge/controller/{v}/MANIFEST.sha256", "file"),
        ("./opt/meshforge/controller/current", "symlink"),
        ("./usr/bin/meshforge-controller", "symlink"),
        ("./usr/lib/systemd/system/meshforge-controller.service", "file"),
        ("./etc/ufw/applications.d/meshforge-controller", "file"),
        ("./usr/share/doc/meshforge-controller/copyright", "file"),
        ("./usr/share/doc/meshforge-controller/changelog.Debian.gz", "file"),
    ),
    current_link="./opt/meshforge/controller/current",
)
SPECS: Final[Mapping[str, PackageSpec]] = {"agent": AGENT, "controller": CONTROLLER}

# The agent package's names, as G1 published them (tests and callers use them).
PACKAGE: Final = AGENT.package
STAGED: Final[Mapping[str, int]] = AGENT.staged
REQUIRED_PATHS: Final = AGENT.required_paths
REQUIRED_CONTROL: Final = ("control", "preinst", "postinst", "prerm", "postrm")


# --------------------------------------------------------------- versions
_PEP440: Final = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_l>a|b|rc)(?P<pre_n>\d+))?"
    r"(?:\.post(?P<post>\d+))?"
    r"(?:\.dev(?P<dev>\d+))?$"
)


def debian_version(version: str, revision: int = 1) -> str:
    """The Debian version of a (normalised) PEP 440 ``version``, ordered the same
    way by dpkg: ``~~devN`` < ``~aN`` < ``~bN`` < ``~rcN`` < final < ``+postN``.
    A ``.devN`` of a pre/post release is ``~devN`` after it (it sorts just before
    that release). Epochs (``N!``) and local versions (``+x``) are refused: a
    shipped package never carries them."""
    if revision < 1:
        raise BundleError("the Debian revision must be >= 1", reason="bad_version")
    match = _PEP440.match(version)
    if match is None:
        raise BundleError(
            f"version {version!r} is not a plain PEP 440 release (no epoch, no "
            "local part): fix [project].version in pyproject.toml",
            reason="bad_version",
        )
    out = match["release"]
    if match["pre_l"]:
        out += f"~{match['pre_l']}{match['pre_n']}"
    if match["post"] is not None:
        out += f"+post{match['post']}"
    if match["dev"] is not None:
        plain = match["pre_l"] is None and match["post"] is None
        out += f"{'~~' if plain else '~'}dev{match['dev']}"
    return f"{out}-{revision}"


# --------------------------------------------------------------- staging
def changelog_gz(
    deb_version: str,
    version: str,
    git_sha: str,
    epoch: int,
    package: str = PACKAGE,
) -> bytes:
    """A one-entry ``changelog.Debian.gz``, reproducible (gzip mtime 0, no name)."""
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(epoch))
    text = (
        f"{package} ({deb_version}) unstable; urgency=medium\n\n"
        f"  * MeshForge {version} (git {git_sha[:12]}).\n\n"
        f" -- {MAINTAINER}  {date}\n"
    )
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9
    ) as gz:
        gz.write(text.encode("utf-8"))
    return buffer.getvalue()


def stage(
    stage_dir: Path,
    packaging: Path,
    changelog: bytes,
    epoch: int,
    staged: Mapping[str, int] = STAGED,
) -> None:
    """Copy the packaging files into ``stage_dir`` with fixed modes + mtimes."""
    for rel, mode in staged.items():
        source = packaging / rel
        if not source.is_file():
            raise BundleError(f"missing packaging file {source}", reason="no_packaging")
        target = stage_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if b"\r\n" in data:
            raise BundleError(
                f"{source} has CRLF line ends (a Windows checkout?): maintainer "
                "scripts and units must be LF",
                reason="no_packaging",
            )
        target.write_bytes(data)
        target.chmod(mode)
        os.utime(target, (epoch, epoch))
    changelog_path = stage_dir / "changelog.Debian.gz"
    changelog_path.write_bytes(changelog)
    changelog_path.chmod(0o644)
    os.utime(changelog_path, (epoch, epoch))


_PLACEHOLDER: Final = re.compile(r"\$\{(MF_[A-Z_]+)\}")
_VALUE_LINE: Final = re.compile(r"^(?P<head>\s*(?:- )?[A-Za-z_]+:[ \t]+)(?P<value>.*)$")


def render_config(template: str, values: Mapping[str, str]) -> str:
    """``nfpm.yaml`` with its ``${MF_*}`` placeholders filled. Each line holding
    a placeholder must be ``key: value``; the filled value is written as a JSON
    (= YAML double-quoted) string. Comment lines are left alone."""
    lines: list[str] = []
    for number, line in enumerate(template.splitlines(), start=1):
        if line.lstrip().startswith("#") or not _PLACEHOLDER.search(line):
            lines.append(line)
            continue
        match = _VALUE_LINE.match(line)
        if match is None:
            raise BundleError(
                f"nfpm.yaml line {number}: a placeholder outside 'key: value'",
                reason="bad_config",
            )

        def fill(found: re.Match[str], number: int = number) -> str:
            name = found[1]
            if name not in values:
                raise BundleError(
                    f"nfpm.yaml line {number}: unknown placeholder ${{{name}}}",
                    reason="bad_config",
                )
            return values[name]

        value = _PLACEHOLDER.sub(fill, match["value"].strip())
        if "${" in value:
            raise BundleError(
                f"nfpm.yaml line {number}: a filled value contains '${{'",
                reason="bad_config",
            )
        lines.append(match["head"] + json.dumps(value))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- inspection
@dataclass(frozen=True, slots=True)
class DebContents:
    members: tuple[str, ...]
    control: dict[str, str]
    control_files: frozenset[str]
    data: Mapping[str, str]
    """path (``./opt/...``) -> ``file`` / ``dir`` / ``symlink:<target>``."""


def read_ar(data: bytes) -> dict[str, bytes]:
    """The members of a Debian ``ar`` archive (common format, no long names)."""
    if not data.startswith(b"!<arch>\n"):
        raise BundleError("not an ar archive (.deb)", reason="bad_deb")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(data):
        header = data[offset : offset + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            raise BundleError("truncated or corrupt .deb (ar header)", reason="bad_deb")
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        members[name] = data[start : start + size]
        offset = start + size + (size % 2)
    return members


def _open_tar(blob: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(blob), mode="r:*")


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def inspect_deb(path: Path) -> DebContents:
    members = read_ar(path.read_bytes())
    order = tuple(members)
    control_name = next((m for m in order if m.startswith("control.tar")), None)
    data_name = next((m for m in order if m.startswith("data.tar")), None)
    if order[:1] != ("debian-binary",) or control_name is None or data_name is None:
        raise BundleError(f"unexpected .deb members {order}", reason="bad_deb")
    control: dict[str, str] = {}
    control_files: set[str] = set()
    with _open_tar(members[control_name]) as tar:
        for info in tar.getmembers():
            if not info.isfile():
                continue
            name = info.name.removeprefix("./")
            control_files.add(name)
            if name == "control":
                handle = tar.extractfile(info)
                if handle is not None:
                    control = parse_control(handle.read().decode("utf-8"))
    data: dict[str, str] = {}
    with _open_tar(members[data_name]) as tar:
        for info in tar.getmembers():
            name = info.name if info.name.startswith("./") else "./" + info.name
            if info.issym():
                data[name.rstrip("/")] = f"symlink:{info.linkname}"
            elif info.isdir():
                data[name.rstrip("/")] = "dir"
            else:
                data[name] = "file"
    return DebContents(order, control, frozenset(control_files), data)


def check_contents(
    contents: DebContents,
    version: str,
    deb_version: str,
    spec: PackageSpec = AGENT,
) -> list[str]:
    """What is wrong with the built package (empty = fine)."""
    problems: list[str] = []
    for field_name, want in (
        ("Package", spec.package),
        ("Version", deb_version),
        ("Architecture", DEB_ARCH),
    ):
        if contents.control.get(field_name) != want:
            problems.append(
                f"control {field_name}: {contents.control.get(field_name)!r} "
                f"(want {want!r})"
            )
    problems.extend(
        f"control.tar has no {name}"
        for name in REQUIRED_CONTROL
        if name not in contents.control_files
    )
    for template, kind in spec.required_paths:
        path = template.format(v=version)
        found = contents.data.get(path)
        if found is None:
            problems.append(f"missing {path}")
        elif kind == "symlink" and not found.startswith("symlink:"):
            problems.append(f"{path} is {found}, want a symlink")
        elif kind == "file" and found != "file":
            problems.append(f"{path} is {found}, want a file")
    current = contents.data.get(spec.current_link, "")
    if current not in ("", f"symlink:{version}"):
        problems.append(f"{spec.current_link[1:]} -> {current[8:]} (want {version})")
    return problems


# ------------------------------------------------------- shared libraries
SYSTEM_LIBS: Final[Mapping[str, str]] = {
    # soname -> the Ubuntu package that ships it (must be in nfpm.yaml depends)
    "libc.so.6": "libc6",
    "libm.so.6": "libc6",
    "libpthread.so.0": "libc6",
    "libdl.so.2": "libc6",
    "librt.so.1": "libc6",
    "libutil.so.1": "libc6",
    "libresolv.so.2": "libc6",  # V0.17 G2: the bundled PostgreSQL (Kerberos, libpq)
    "linux-vdso.so.1": "libc6",
    "libcrypt.so.1": "libcrypt1",
    "libgcc_s.so.1": "libgcc-s1",
    "libstdc++.so.6": "libstdc++6",
    "libz.so.1": "zlib1g",
}
"""Every library the bundle may take from the system. Anything else must be
inside the bundle (wheels vendor theirs): a new one fails the build, so a
dependency bump cannot silently need a package the .deb does not declare."""
_ELF_MAGIC: Final = b"\x7fELF"
_LDD_ADDRESS: Final = re.compile(r" \(0x[0-9a-f]+\)$")


def elf_files(bundle: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) == _ELF_MAGIC:
                found.append(path)
    return found


def classify_ldd(output: str, bundle: Path) -> tuple[set[str], list[str]]:
    """``ldd`` output for bundle files -> (system packages needed, problems).
    Pure: a library inside ``bundle`` is fine, a known system soname adds its
    package, an unknown one or ``not found`` is a problem."""
    packages: set[str] = set()
    problems: list[str] = []
    current = ""
    root = os.path.realpath(bundle)
    for raw in output.splitlines():
        line = raw.strip()
        if raw and not raw.startswith(("\t", " ")) and line.endswith(":"):
            current = line[:-1]
            continue
        if "=>" not in line:
            name = line.split(" ", 1)[0]
            if name in SYSTEM_LIBS:
                packages.add(SYSTEM_LIBS[name])
            continue
        name, _, rest = line.partition("=>")
        name = name.strip()
        # "<path with spaces> (0x...)" or "not found"
        target = _LDD_ADDRESS.sub("", rest.strip())
        if target == "not found":
            problems.append(f"{current}: {name} not found")
        elif os.path.realpath(target).startswith(root + os.sep):
            continue
        elif name in SYSTEM_LIBS:
            packages.add(SYSTEM_LIBS[name])
        else:
            problems.append(f"{current}: needs {name} from the system ({target})")
    return packages, problems


def check_shared_libs(bundle: Path, depends: Sequence[str]) -> list[str]:
    """``ldd`` every ELF file of the bundle; problems (empty = only declared
    system libraries are used). No ``ldd`` -> one problem saying so."""
    ldd = shutil.which("ldd")
    if ldd is None:
        return ["ldd is not installed: cannot check the bundle's system libraries"]
    output: list[str] = []
    files = [str(p) for p in elf_files(bundle)]
    for start in range(0, len(files), 200):
        chunk = files[start : start + 200]
        done = subprocess.run(
            [ldd, *chunk], capture_output=True, text=True, check=False, timeout=300
        )
        text = done.stdout
        if len(chunk) == 1:  # ldd names the file only when given several
            text = f"{chunk[0]}:\n{text}"
        output.append(text)
    packages, problems = classify_ldd("\n".join(output), bundle)
    declared = {d.split(" ", 1)[0] for d in depends}
    problems += [
        f"the bundle needs {pkg} but nfpm.yaml does not depend on it"
        for pkg in sorted(packages - declared)
    ]
    return problems


def nfpm_depends(template: str) -> list[str]:
    """The ``depends:`` list of ``nfpm.yaml`` (a plain list of ``- item`` lines)."""
    items: list[str] = []
    inside = False
    for line in template.splitlines():
        if line.startswith("depends:"):
            inside = True
            continue
        if inside:
            if line.startswith("  - "):
                items.append(line[4:].strip())
            elif line.strip() and not line.lstrip().startswith("#"):
                break
    return items


# ----------------------------------------------------------------- nfpm
def nfpm_binary(pins: Mapping[str, object], cache: Path) -> Path:
    """The pinned nfpm, unpacked once into ``cache/tools/nfpm-<version>/``."""
    import build_bundle  # the shared, sha256-checking fetch

    download = pinned(pins, "nfpm", "linux-x86_64")
    tool = cache / "tools" / f"nfpm-{download.version}" / "nfpm"
    if tool.is_file():
        return tool
    archive = build_bundle.fetch(download, cache)
    tool.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.getmember("nfpm")
        except KeyError:
            raise BundleError(
                f"{archive.name} has no 'nfpm' binary", reason="bad_tool"
            ) from None
        handle = tar.extractfile(member)
        if handle is None:
            raise BundleError(
                f"{archive.name}: 'nfpm' is not a file", reason="bad_tool"
            )
        part = tool.with_name("nfpm.part")
        part.write_bytes(handle.read())
    part.chmod(0o755)
    part.rename(tool)
    return tool


# ----------------------------------------------------------------- the build
@dataclass(frozen=True, slots=True)
class DebOptions:
    bundle: Path
    out: Path
    cache: Path
    revision: int = 1
    force: bool = False
    size_budget: bool = True
    check_libs: bool = True
    pins: Path = PINS
    packaging: Path | None = None
    """None = the role's folder (``PackageSpec.packaging``)."""
    role: str = "agent"


def log(message: str) -> None:
    print(f"build_deb: {message}", file=sys.stderr)


def bundle_facts(bundle: Path, role: str = "agent") -> dict[str, object]:
    try:
        info = json.loads((bundle / BUILDINFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            f"{bundle} is not a built bundle ({BUILDINFO_NAME}: {exc})",
            reason="no_bundle",
        ) from None
    if not isinstance(info, dict):
        raise BundleError(
            f"{bundle}/{BUILDINFO_NAME} is not an object", reason="no_bundle"
        )
    want = {"role": role, "os": "linux", "arch": "x86_64"}
    wrong = {k: info.get(k) for k, v in want.items() if info.get(k) != v}
    if wrong:
        raise BundleError(
            f"{bundle} is not a linux x86_64 {role} bundle ({wrong}); build one "
            f"with scripts/build_bundle.py --role {role}",
            reason="wrong_bundle",
        )
    for key in ("version", "source_date_epoch", "git_sha"):
        if key not in info:
            raise BundleError(f"{BUILDINFO_NAME} has no {key}", reason="no_bundle")
    return info


def build_deb(options: DebOptions) -> Path:
    spec = SPECS[options.role]
    packaging = options.packaging or spec.packaging
    bundle = options.bundle.resolve()
    info = bundle_facts(bundle, spec.role)
    differences = verify_bundle(bundle)
    if differences:
        shown = "\n".join(f"  {d}" for d in differences[:20])
        raise BundleError(
            f"{bundle} differs from its MANIFEST.sha256 ({len(differences)}):\n{shown}",
            reason="bundle_modified",
        )
    version = str(info["version"])
    epoch = int(str(info["source_date_epoch"]))
    deb_version = debian_version(version, options.revision)
    out = options.out.resolve()
    target = out / f"{spec.package}_{deb_version}_{DEB_ARCH}.deb"
    if target.exists() and not options.force:
        raise BundleError(f"{target} exists (use --force)", reason="exists")
    template = (packaging / "nfpm.yaml").read_text(encoding="utf-8")
    if options.check_libs:
        problems = check_shared_libs(bundle, nfpm_depends(template))
        if problems:
            raise BundleError(
                "the bundle's shared libraries do not match the package's Depends:\n"
                + "\n".join(f"  {p}" for p in problems[:30]),
                reason="system_libs",
            )
    pins = load_pins(options.pins)
    budget_table = pins.get("size_budget_mb")
    budget = budget_table.get(spec.role) if isinstance(budget_table, dict) else None
    nfpm = nfpm_binary(pins, options.cache)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".deb-", dir=out) as tmp:
        stage_dir = Path(tmp) / "stage"
        stage_dir.mkdir()
        stage(
            stage_dir,
            packaging,
            changelog_gz(
                deb_version, version, str(info["git_sha"]), epoch, spec.package
            ),
            epoch,
            spec.staged,
        )
        config = Path(tmp) / "nfpm.yaml"
        config.write_text(
            render_config(
                template,
                {
                    "MF_DEB_VERSION": deb_version,
                    "MF_VERSION": version,
                    "MF_BUNDLE": str(bundle),
                    "MF_STAGE": str(stage_dir),
                    "MF_MTIME": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
                },
            ),
            encoding="utf-8",
        )
        built = Path(tmp) / target.name
        import build_bundle

        log(f"packaging {target.name} (nfpm {nfpm.parent.name.split('-', 1)[1]})")
        build_bundle.run(
            [
                nfpm,
                "package",
                "--config",
                config,
                "--packager",
                "deb",
                "--target",
                built,
            ],
            env=build_bundle.clean_env({"SOURCE_DATE_EPOCH": str(epoch)}),
            what="nfpm package",
            cwd=Path(tmp),
        )
        problems = check_contents(inspect_deb(built), version, deb_version, spec)
        if problems:
            raise BundleError(
                "the built package is incomplete:\n"
                + "\n".join(f"  {p}" for p in problems),
                reason="bad_deb",
            )
        size = built.stat().st_size
        if options.size_budget and isinstance(budget, int | float):
            check_size_budget(size, float(budget), target.name)
        built.replace(target)

    digest = sha256_file(target)
    sidecar = target.with_name(target.name + ".sha256")
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    log(f"built {target} ({size / 1e6:.1f} MB, sha256 {digest[:16]}...)")
    return target


# ----------------------------------------------------------------------- CLI
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path, help="a built bundle folder")
    source.add_argument(
        "--build-bundle",
        action="store_true",
        help="build the role's bundle into --out first (build_bundle.py --role ...)",
    )
    parser.add_argument(
        "--role",
        choices=sorted(SPECS),
        default="agent",
        help="which package: meshforge-agent (default) or meshforge-controller",
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--revision",
        type=int,
        default=1,
        help="Debian revision (the -N suffix); bump it to repackage one bundle",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "meshforge-bundle",
        help="downloads cache (shared with build_bundle.py)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing .deb")
    parser.add_argument(
        "--no-size-budget", action="store_true", help="do not fail on the .deb size"
    )
    parser.add_argument("--pins", type=Path, default=PINS, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.platform != "linux":
        print(
            "build_deb: error (unsupported): build the .deb on Linux", file=sys.stderr
        )
        return 1
    try:
        bundle = args.bundle
        if args.build_bundle:
            import build_bundle

            bundle = build_bundle.build(
                build_bundle.BuildOptions(
                    role=args.role,
                    out=args.out,
                    cache=args.cache,
                    force=args.force,
                    pins=args.pins,
                )
            )
        build_deb(
            DebOptions(
                bundle=bundle,
                out=args.out,
                cache=args.cache,
                revision=args.revision,
                force=args.force,
                size_budget=not args.no_size_budget,
                pins=args.pins,
                role=args.role,
            )
        )
    except BundleError as exc:
        print(f"build_deb: error ({exc.reason}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
