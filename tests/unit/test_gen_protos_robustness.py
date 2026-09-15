"""Tester (task 0.1.5): adversarial coverage of V0.1 success criterion 4,

    "Proto codegen is deterministic (regenerating produces no diff)."

beyond what ``tests/unit/test_protos.py`` already checks. Everything here runs
against a throwaway copy of ``proto/`` and the committed generated tree
(``repo_copy`` fixture, mirrored from ``test_protos.py``); nothing under
``src/``, ``proto/`` or ``scripts/`` in the real repository is ever touched.

Covers:
- regenerating N times in a row is byte-identical every time (not just once);
- the script does not depend on the current working directory
  (``--repo-root`` is honoured even when invoked from elsewhere), and the real
  repository path already contains a space ("FINAL YEAR") which every
  subprocess invocation below exercises implicitly;
- CRLF line endings in the *source* .proto files (as a naive Windows
  ``core.autocrlf=true`` checkout would produce) still yield byte-identical,
  LF-only generated output;
- adding a brand-new .proto file is picked up, generated, and has its
  cross-file import rewritten;
- removing a .proto file deletes its generated files (real removal, not a
  synthetic stale file);
- a proto importing ``google/protobuf/timestamp.proto`` resolves;
- ``--check`` against a repo copy whose generated-output directory does not
  exist at all fails loudly instead of silently treating "missing dir" as
  "up to date".
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_protos.py"
OUT_DIR = REPO_ROOT / "src" / "meshforge" / "common" / "proto"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Minimal copy of the repo containing what gen_protos.py reads and writes.

    ``tmp_path`` itself lives under the pytest tmp root, which on this machine
    does not contain a space; the *real* repository root
    ("FINAL YEAR") does, and every subprocess call in this module that omits
    ``--repo-root`` (i.e. runs against the real committed tree) already proves
    the script tolerates a space in its own path.
    """
    shutil.copytree(REPO_ROOT / "proto", tmp_path / "proto")
    shutil.copytree(
        OUT_DIR,
        tmp_path / "src" / "meshforge" / "common" / "proto",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # Package skeleton above ``common/proto`` so that ``sys.path.insert(0, "src")``
    # against *this* copy resolves ``meshforge`` / ``meshforge.common`` to the
    # copy rather than falling through to the editable-installed real repo (whose
    # ``meshforge.common.proto.meshforge.v1`` package would otherwise shadow any
    # module -- e.g. a freshly added proto -- that only exists in the copy).
    shutil.copy2(
        REPO_ROOT / "src" / "meshforge" / "__init__.py",
        tmp_path / "src" / "meshforge" / "__init__.py",
    )
    shutil.copy2(
        REPO_ROOT / "src" / "meshforge" / "common" / "__init__.py",
        tmp_path / "src" / "meshforge" / "common" / "__init__.py",
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def _tree(out_dir: Path) -> dict[Path, bytes]:
    return {
        p.relative_to(out_dir): p.read_bytes()
        for p in out_dir.rglob("*")
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# Determinism, repeated + CWD independence
# ---------------------------------------------------------------------------


def test_regeneration_is_byte_identical_across_five_runs(repo_copy: Path) -> None:
    out = repo_copy / "src" / "meshforge" / "common" / "proto"
    baseline = _tree(out)
    for i in range(5):
        result = _run("--repo-root", str(repo_copy))
        assert result.returncode == 0, f"run {i}: {result.stderr}"
        assert "0 changed" in result.stdout, f"run {i}: {result.stdout}"
        assert _tree(out) == baseline, f"run {i} produced a different tree"


def test_generate_from_a_different_cwd(
    repo_copy: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``--repo-root`` must be honoured regardless of the process CWD."""
    elsewhere = tmp_path_factory.mktemp("elsewhere with space")
    out = repo_copy / "src" / "meshforge" / "common" / "proto"
    baseline = _tree(out)
    result = _run("--check", "--repo-root", str(repo_copy), cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert _tree(out) == baseline


def test_default_repo_root_works_when_invoked_from_elsewhere(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Without --repo-root, the script must resolve the repo root relative to
    its own file location (``parents[1]``), not the process CWD."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    result = _run("--check", cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout


# ---------------------------------------------------------------------------
# CRLF source protos (simulated Windows checkout, core.autocrlf=true)
# ---------------------------------------------------------------------------


def test_crlf_source_protos_still_produce_identical_lf_output(repo_copy: Path) -> None:
    proto_dir = repo_copy / "proto" / "meshforge" / "v1"
    for proto in sorted(proto_dir.glob("*.proto")):
        text = proto.read_text(encoding="utf-8")
        assert "\r" not in text, "fixture precondition: source protos are LF already"
        proto.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        assert b"\r\n" in proto.read_bytes()

    out = repo_copy / "src" / "meshforge" / "common" / "proto"
    committed = _tree(out)

    result = _run("--repo-root", str(repo_copy))
    assert result.returncode == 0, result.stderr
    assert "0 changed" in result.stdout, (
        "CRLF-converted source .proto files must not change generated output: "
        f"{result.stdout}"
    )
    assert _tree(out) == committed

    for path in out.rglob("*"):
        if path.is_file():
            assert b"\r" not in path.read_bytes(), f"{path} retained CR bytes"


# ---------------------------------------------------------------------------
# Adding / removing .proto files
# ---------------------------------------------------------------------------

_NEW_PROTO = """\
// Scratch proto added by a tester run to prove new files are picked up.
syntax = "proto3";

package meshforge.v1;

import "meshforge/v1/common.proto";

message ExtraThing {
  string id = 1;
  Failure failure = 2;
}
"""


def test_adding_a_new_proto_is_generated_with_rewritten_imports(
    repo_copy: Path,
) -> None:
    new_proto = repo_copy / "proto" / "meshforge" / "v1" / "extra.proto"
    new_proto.write_text(_NEW_PROTO, encoding="utf-8")

    check_before = _run("--check", "--repo-root", str(repo_copy))
    assert check_before.returncode == 1
    assert "missing: meshforge/v1/extra_pb2.py" in check_before.stderr

    gen = _run("--repo-root", str(repo_copy))
    assert gen.returncode == 0, gen.stderr

    out = repo_copy / "src" / "meshforge" / "common" / "proto" / "meshforge" / "v1"
    generated = (out / "extra_pb2.py").read_bytes()
    assert b"from meshforge.v1 import" not in generated
    assert b"from meshforge.common.proto.meshforge.v1 import common_pb2" in generated
    assert (out / "extra_pb2.pyi").exists()

    again = _run("--check", "--repo-root", str(repo_copy))
    assert again.returncode == 0, again.stderr

    # Sanity: the new module is actually importable and links to common_pb2.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, 'src'); "
                "from meshforge.common.proto.meshforge.v1 import extra_pb2, common_pb2; "
                "m = extra_pb2.ExtraThing(id='x', failure=common_pb2.Failure(message='m')); "
                "assert type(m.failure) is common_pb2.Failure; print('IMPORT_OK')"
            ),
        ],
        cwd=repo_copy,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_removing_a_proto_deletes_its_generated_files(repo_copy: Path) -> None:
    (repo_copy / "proto" / "meshforge" / "v1" / "gpu.proto").unlink()

    out = repo_copy / "src" / "meshforge" / "common" / "proto" / "meshforge" / "v1"
    assert (out / "gpu_pb2.py").exists()

    check = _run("--check", "--repo-root", str(repo_copy))
    assert check.returncode == 1
    for rel in ("gpu_pb2.py", "gpu_pb2.pyi", "gpu_pb2_grpc.py", "gpu_pb2_grpc.pyi"):
        assert f"stale (not generated from protos): meshforge/v1/{rel}" in check.stderr

    gen = _run("--repo-root", str(repo_copy))
    assert gen.returncode == 0, gen.stderr
    for rel in ("gpu_pb2.py", "gpu_pb2.pyi", "gpu_pb2_grpc.py", "gpu_pb2_grpc.pyi"):
        assert not (out / rel).exists(), f"{rel} should have been removed"
    # common.proto's own outputs must survive (only gpu.proto's are stale).
    assert (out / "common_pb2.py").exists()

    again = _run("--check", "--repo-root", str(repo_copy))
    assert again.returncode == 0, again.stderr


# ---------------------------------------------------------------------------
# well-known-type import resolution
# ---------------------------------------------------------------------------


def test_wellknown_timestamp_import_resolves() -> None:
    """agent.proto imports google/protobuf/timestamp.proto; the field must be
    the real WKT class, proving -I<grpc_tools wkt dir> resolves correctly."""
    from google.protobuf.timestamp_pb2 import Timestamp

    from meshforge.common.proto.meshforge.v1 import agent_pb2

    msg = agent_pb2.AgentMessage()
    assert isinstance(msg.sent_at, Timestamp)
    field = agent_pb2.AgentMessage.DESCRIPTOR.fields_by_name["sent_at"]
    assert field.message_type is not None
    assert field.message_type.full_name == "google.protobuf.Timestamp"


# ---------------------------------------------------------------------------
# --check with a missing output directory
# ---------------------------------------------------------------------------


def test_check_with_missing_output_dir_reports_everything_missing(
    repo_copy: Path,
) -> None:
    shutil.rmtree(repo_copy / "src" / "meshforge" / "common" / "proto")
    result = _run("--check", "--repo-root", str(repo_copy))
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "missing: meshforge/v1/common_pb2.py" in result.stderr
    assert "missing: meshforge/v1/agent_pb2.py" in result.stderr
    assert "missing: meshforge/v1/gpu_pb2.py" in result.stderr

    gen = _run("--repo-root", str(repo_copy))
    assert gen.returncode == 0, gen.stderr
    again = _run("--check", "--repo-root", str(repo_copy))
    assert again.returncode == 0, again.stderr
