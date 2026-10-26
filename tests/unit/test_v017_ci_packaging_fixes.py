"""CI fixes (2026-10-27) for the package jobs.

* package-linux: lintian ``shared-library-is-executable`` (an error) on
  ``pgsql/lib/libpq.so.5`` - the upstream PostgreSQL archive ships it 0755 and
  the bundle's mode normalisation keeps an execute bit. ``install_pgsql`` now
  drops the execute bits of ``pgsql/lib`` files.
* package-windows: ``Get-Acl`` in a ``powershell.exe`` started from a ``pwsh``
  step failed ``CouldNotAutoloadMatchingModule`` (PowerShell 7's module path
  inherited). The lab drivers start Windows PowerShell without ``PSModulePath``.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str, extra_path: Path | None = None) -> ModuleType:
    if extra_path is not None and str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
    if name in sys.modules:  # loaded by another test module: share it
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_install_pgsql_ships_shared_libraries_without_execute_bits(
    tmp_path: Path,
) -> None:
    pg_bundle = _load(REPO / "scripts" / "pg_bundle.py", "pg_bundle", REPO / "scripts")
    trimmed = tmp_path / "trimmed"
    (trimmed / "bin").mkdir(parents=True)
    (trimmed / "lib").mkdir()
    postgres = trimmed / "bin" / "postgres"
    postgres.write_bytes(b"\x7fELF")
    postgres.chmod(0o755)
    libpq = trimmed / "lib" / "libpq.so.5"
    libpq.write_bytes(b"\x7fELF")
    libpq.chmod(0o755)
    module = trimmed / "lib" / "plpgsql.so"
    module.write_bytes(b"\x7fELF")
    module.chmod(0o775)
    plain = trimmed / "lib" / "libz.so.1"
    plain.write_bytes(b"\x7fELF")
    plain.chmod(0o644)
    (trimmed / "lib" / "libpq.so").symlink_to("libpq.so.5")
    (trimmed / pg_bundle.MARKER).write_text("{}\n")
    bundle = pg_bundle.TrimmedBundle(
        root=trimmed,
        full_root=trimmed,
        archive=trimmed,
        archive_bytes=0,
        full_bytes=0,
        trimmed_bytes=0,
    )
    root = tmp_path / "bundle"
    root.mkdir()
    dest = pg_bundle.install_pgsql(bundle, root)

    def mode(p: Path) -> int:
        return stat.S_IMODE(p.lstat().st_mode)

    assert mode(dest / "lib" / "libpq.so.5") == 0o644
    assert mode(dest / "lib" / "plpgsql.so") == 0o664
    assert mode(dest / "lib" / "libz.so.1") == 0o644
    assert (dest / "lib" / "libpq.so").is_symlink()
    assert mode(dest / "bin" / "postgres") == 0o755  # programs stay executable
    assert not (dest / pg_bundle.MARKER).exists()
    # idempotent, and a missing folder is not an error
    assert pg_bundle.drop_library_exec_bits(dest / "lib") == 0
    assert pg_bundle.drop_library_exec_bits(tmp_path / "missing") == 0


def test_windows_powershell_runs_without_the_inherited_module_path() -> None:
    environ = {
        "PATH": r"C:\Windows\System32",
        "PSModulePath": r"C:\Program Files\PowerShell\7\Modules;C:\x",
        "SystemRoot": r"C:\Windows",
    }
    package_check = _load(
        REPO / "lab" / "windows" / "package_check.py", "mf_package_check"
    )
    verify_uninstall = _load(REPO / "lab" / "verify_uninstall.py", "mf_verify_uninst")
    for module in (package_check, verify_uninstall):
        env = module.windows_powershell_env(environ)
        assert env == {"PATH": r"C:\Windows\System32", "SystemRoot": r"C:\Windows"}
    # case-insensitive, like Windows environment names
    assert package_check.windows_powershell_env({"PSMODULEPATH": "x"}) == {}
    assert "PSModulePath" in environ  # the caller's mapping is not changed


def test_acl_check_reads_through_dotnet_not_the_get_acl_cmdlet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_check = _load(
        REPO / "lab" / "windows" / "package_check.py", "mf_package_check"
    )
    seen: list[str] = []

    def fake_ps(script: str) -> str:
        seen.append(script)
        return "S-1-5-18\tFullControl\tAllow\r\nS-1-5-32-544\tFullControl\tAllow\r\n"

    monkeypatch.setattr(package_check, "ps", fake_ps)
    found = package_check.acl_sids(Path(r"C:\ProgramData\Mesh'Forge\console\token"))
    assert found == {"S-1-5-18": "FullControl", "S-1-5-32-544": "FullControl"}
    assert "Get-Acl" not in seen[0] and "GetAccessControl" in seen[0]
    assert "Mesh''Forge" in seen[0]  # quoted for a PowerShell literal

    monkeypatch.setattr(package_check, "ps", lambda _s: "S-1-1-0\tRead\tDeny\n")
    with pytest.raises(package_check.CheckError):
        package_check.acl_sids(Path(r"C:\x"))
