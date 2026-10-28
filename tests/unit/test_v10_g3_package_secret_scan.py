"""V1.0 G3 (D413, reviewer M3): ``scripts/package_secret_scan.py``.

Planted secrets in every artefact shape the builders produce (a bundle folder,
a ``.deb`` with control + data archives, a ``.tar.xz``, a wheel/zip, a raw
file): each rule fires, the report never contains the secret, and look-alikes
that legitimately ship (the redaction regexes, ``DEV_DB_PASSWORD`` in Python
source, third-party test suites, public CA bundles) do not. The three builders
are pinned to call the scan and fail the build on a hit.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import secrets
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pss = _load("package_secret_scan")
lib = _load("bundle_lib")

SITE = "python/lib/python3.12/site-packages"


def _pem_key() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


API_TOKEN = ("mfat_" + secrets.token_urlsafe(32)).encode()
SESSION = ("mfbs_" + secrets.token_urlsafe(32)).encode()
JOIN_CODE = b"mf1-aeaqc-aaaab-hcaaa-baaaa-qcaaa-aqaaa-aaqca-aaaab"


def _rules(hits: list[object]) -> set[str]:
    return {h.rule for h in hits}  # type: ignore[attr-defined]


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


# ----------------------------------------------------------------- the rules
@pytest.mark.parametrize(
    ("member", "data", "rule"),
    [
        (f"{SITE}/x/cfg.py", b"K = '''" + b"%PEM%" + b"'''", "pem_private_key"),
        (f"{SITE}/x/a.txt", b"token " + API_TOKEN + b"\n", "api_token"),
        (f"{SITE}/x/a.json", b'{"s": "' + SESSION + b'"}', "session_token"),
        (f"{SITE}/x/a.md", b"join with " + JOIN_CODE, "join_code"),
        (
            "etc/meshforge/controller.toml",
            b'password = "meshforge-dev"\n',
            "dev_password",
        ),
        ("x/.env", b"MESHFORGE_DATABASE__PASSWORD=meshforge-dev\n", "dev_password"),
        (f"{SITE}/m/t.py", b"PW = 'correct horse battery staple 42'", "dev_password"),
    ],
    ids=[
        "pem",
        "api-token",
        "session",
        "join-code",
        "dev-pw-toml",
        "dev-pw-env",
        "test-pw",
    ],
)
def test_each_content_rule_fires_and_never_prints_the_secret(
    tmp_path: Path,
    member: str,
    data: bytes,
    rule: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pem = _pem_key()
    data = data.replace(b"%PEM%", pem)
    root = _tree(tmp_path / "bundle", {member: data})
    hits = pss.scan(root)
    assert rule in _rules(hits), hits
    assert pss.main([str(root)]) == 1
    out = capsys.readouterr()
    text = out.out + out.err
    for secret in (
        pem.decode(),
        API_TOKEN.decode(),
        SESSION.decode(),
        JOIN_CODE.decode(),
    ):
        assert secret not in text
    assert "meshforge-dev" not in text and "correct horse" not in text


@pytest.mark.parametrize(
    "member",
    [
        "controller/fabric.token",
        "controller/secrets.key",
        "controller/peer_signing.key",
        "identity/node.key",
        "controller/setup-token",
        "db/app.password",
        "db/admin.password",
        "home/.pgpass",
        "home/.ssh/id_ed25519",
        "x/.env",
    ],
)
def test_secret_file_names(tmp_path: Path, member: str) -> None:
    root = _tree(tmp_path / "b", {member: b"\x00"})
    assert "secret_file" in _rules(pss.scan(root))


@pytest.mark.parametrize(
    "member",
    [
        "tests/unit/test_x.py",
        f"{SITE}/tests/conftest.py",
        f"{SITE}/tests/__init__.py",
        f"{SITE}/meshforge/tests/test_a.py",
        f"{SITE}/meshforge/controller/test/x.py",
        f"{SITE}/meshforge/conftest.py",
        "meshforge/tests/test_wheel.py",  # a wheel's members start at the package
        "conftest.py",
    ],
)
def test_meshforge_test_material(tmp_path: Path, member: str) -> None:
    root = _tree(tmp_path / "b", {member: b"x = 1\n"})
    assert "test_dir" in _rules(pss.scan(root)), member


@pytest.mark.parametrize(
    ("member", "data"),
    [
        # the real redaction regex and token-shape regex as shipped
        (
            f"{SITE}/meshforge/common/redaction.py",
            rb'_PEM = re.compile(r"-----BEGIN (?P<label>[A-Z0-9 ]{0,40}PRIVATE KEY)-----")',
        ),
        (
            f"{SITE}/meshforge/common/api_token.py",
            rb'_SHAPE = re.compile(r"mfat_[A-Za-z0-9_-]{43}")',
        ),
        (f"{SITE}/meshforge/common/join_code.py", b"``mf1-xxxxx-xxxxx-...``"),
        # a PEM marker with no body (docs, a parser's constant)
        (
            f"{SITE}/cryptography/ssh.py",
            b'_SK = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n',
        ),
        # the dev password named in Python source on purpose (refused at start)
        (
            f"{SITE}/meshforge/controller/store/settings.py",
            b'DEV_DB_PASSWORD: Final = "meshforge-dev"\n',
        ),
        # third-party test suites are not MeshForge's
        (f"{SITE}/numpy/tests/test_core.py", b"x = 1\n"),
        (
            f"{SITE}/grpc/_cython/_credentials/roots.pem",
            b"-----BEGIN CERTIFICATE-----\nAAAA\n",
        ),
        # a token prefix with a too-short tail, or a longer word
        (f"{SITE}/meshforge/x.py", b"mfat_short and mfat_" + b"a" * 60),
        # the deb path segment 'meshforge' is not the meshforge package
        (
            "opt/meshforge/agent/current/python/lib/python3.12/site-packages/numpy/tests/t.py",
            b"",
        ),
    ],
)
def test_look_alikes_that_ship_are_not_hits(
    tmp_path: Path, member: str, data: bytes
) -> None:
    root = _tree(tmp_path / "b", {member: data})
    assert pss.scan(root) == []


# ------------------------------------------------------------ artefact shapes
def _tar_bytes(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:  # type: ignore[call-overload]
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ar(members: dict[str, bytes]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members.items():
        header = (
            f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}".encode()
            + b"`\n"
        )
        out += header + data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def test_a_deb_is_scanned_in_both_archives(tmp_path: Path) -> None:
    deb = tmp_path / "meshforge-agent_1.0-1_amd64.deb"
    deb.write_bytes(
        _ar(
            {
                "debian-binary": b"2.0\n",
                "control.tar.gz": _tar_bytes({"./postinst": b"echo " + API_TOKEN}),
                "data.tar.xz": _tar_bytes(
                    {"./var/lib/meshforge/controller/fabric.token": b"t"}, "w:xz"
                ),
            }
        )
    )
    hits = pss.scan(deb)
    assert {(h.member, h.rule) for h in hits} == {
        ("control/postinst", "api_token"),
        ("var/lib/meshforge/controller/fabric.token", "secret_file"),
    }


def test_a_tar_xz_bundle_archive(tmp_path: Path) -> None:
    archive = tmp_path / "meshforge-1.0-linux-x86_64.tar.xz"
    archive.write_bytes(
        _tar_bytes(
            {f"m/{SITE}/meshforge/tests/test_x.py": b"", "m/key.pem": _pem_key()},
            "w:xz",
        )
    )
    assert _rules(pss.scan(archive)) == {"test_dir", "pem_private_key"}


def test_a_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "meshforge-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("meshforge/tests/test_x.py", "")
        zf.writestr("meshforge/web/assets/app.js", b"const t='" + SESSION + b"'")
    assert _rules(pss.scan(wheel)) == {"test_dir", "session_token"}


def test_a_raw_file_such_as_the_installer(tmp_path: Path) -> None:
    exe = tmp_path / "MeshForge-Agent-Setup-1.0-1.exe"
    exe.write_bytes(b"MZ\x00" + JOIN_CODE + b"\x00")
    assert _rules(pss.scan(exe)) == {"join_code"}


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "b", {f"{SITE}/meshforge/__init__.py": b"__version__ = '1'\n"}
    )
    assert pss.scan(root) == []
    assert pss.main([str(root)]) == 0


def test_missing_or_corrupt_artefact_is_exit_2(tmp_path: Path) -> None:
    assert pss.main([str(tmp_path / "nope.deb")]) == 2
    bad = tmp_path / "x.deb"
    bad.write_bytes(b"not an ar archive")
    assert pss.main([str(bad)]) == 2
    broken = tmp_path / "y.tar.xz"
    broken.write_bytes(b"\xfd7zXZ\x00garbage")
    assert pss.main([str(broken)]) == 2


def test_json_output_has_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path / "b", {"a.txt": API_TOKEN})
    assert pss.main([str(root), "--json"]) == 1
    out = capsys.readouterr().out
    assert '"rule": "api_token"' in out and API_TOKEN.decode() not in out


# ------------------------------------------------------------ builder wiring
def test_check_no_secrets_fails_the_build(tmp_path: Path) -> None:
    root = _tree(tmp_path / "b", {"controller/secrets.key": b"k"})
    with pytest.raises(lib.BundleError) as raised:
        lib.check_no_secrets(root)
    assert raised.value.reason == "secret_found"
    assert "secrets.key" in str(raised.value)
    with pytest.raises(lib.BundleError) as missing:
        lib.check_no_secrets(tmp_path / "gone")
    assert missing.value.reason == "secret_scan"
    lib.check_no_secrets(_tree(tmp_path / "ok", {"a.txt": b"fine"}))


@pytest.mark.parametrize(
    ("script", "function"),
    [
        ("build_bundle.py", "build"),
        ("build_deb.py", "build_deb"),
        ("build_windows_installer.py", "build"),
    ],
)
def test_every_builder_calls_the_scan(script: str, function: str) -> None:
    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    (func,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == function
    ]
    calls = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "check_no_secrets"
    ]
    assert calls, f"{script}:{function} no longer scans its artefact"
