"""``compute.yaml`` (brief D46): bounds, typed errors with YAML paths, inference, no shell."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.manifest import (
    DEFAULT_TIMEOUT_S,
    MANIFEST_FILE_NAME,
    MAX_ARG_CHARS,
    MAX_ARGV_ITEMS,
    MAX_CPUS,
    MAX_MANIFEST_BYTES,
    MAX_NAME_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_OUTPUTS,
    MAX_RAM_GB,
    MAX_TIMEOUT_S,
    MAX_VRAM_GB,
    Manifest,
    ManifestError,
    load_manifest,
    parse_duration,
    parse_manifest_data,
    parse_manifest_text,
    sanitize_name,
)

BASE: dict[str, Any] = {"entrypoint": ["python", "main.py"]}


def _parse(**fields: Any) -> Manifest:
    return parse_manifest_data({**BASE, **fields})


def _paths(exc: pytest.ExceptionInfo[ManifestError]) -> list[str]:
    return [path for path, _ in exc.value.problems]


# ------------------------------------------------------------------ bounds


@pytest.mark.parametrize(
    ("fields", "ok"),
    [
        ({"name": "a" * MAX_NAME_CHARS}, True),
        ({"name": "a" * (MAX_NAME_CHARS + 1)}, False),
        ({"entrypoint": ["p"] * MAX_ARGV_ITEMS}, True),
        ({"entrypoint": ["p"] * (MAX_ARGV_ITEMS + 1)}, False),
        ({"entrypoint": ["p", "x" * MAX_ARG_CHARS]}, True),
        ({"entrypoint": ["p", "x" * (MAX_ARG_CHARS + 1)]}, False),
        ({"outputs": [f"o{i}" for i in range(MAX_OUTPUTS)]}, True),
        ({"outputs": [f"o{i}" for i in range(MAX_OUTPUTS + 1)]}, False),
        ({"outputs": ["o" * MAX_OUTPUT_CHARS]}, True),
        ({"outputs": ["o" * (MAX_OUTPUT_CHARS + 1)]}, False),
        ({"resources": {"gpu": "required", "vram_gb": MAX_VRAM_GB}}, True),
        ({"resources": {"gpu": "required", "vram_gb": MAX_VRAM_GB + 0.001}}, False),
        ({"resources": {"ram_gb": MAX_RAM_GB}}, True),
        ({"resources": {"ram_gb": MAX_RAM_GB + 0.001}}, False),
        ({"resources": {"cpus": MAX_CPUS}}, True),
        ({"resources": {"cpus": MAX_CPUS + 0.001}}, False),
        ({"resources": {"cpus": 0}}, False),
        ({"resources": {"ram_gb": -1}}, False),
        ({"limits": {"timeout": MAX_TIMEOUT_S}}, True),
        ({"limits": {"timeout": MAX_TIMEOUT_S + 1}}, False),
        ({"limits": {"timeout": 1}}, True),
        ({"limits": {"timeout": 0}}, False),
        ({"limits": {"timeout": "7d"}}, True),
        ({"limits": {"timeout": "7d1s"}}, False),
    ],
)
def test_every_bound_max_accepted_and_max_plus_one_refused(
    fields: dict[str, Any], ok: bool
) -> None:
    if ok:
        _parse(**fields)
    else:
        with pytest.raises(ManifestError) as info:
            _parse(**fields)
        assert info.value.failure_type is FailureType.INVALID_INPUT


def test_manifest_file_size_cap(tmp_path: Path) -> None:
    body = "entrypoint: python main.py\n"
    padded = body + "#" * (MAX_MANIFEST_BYTES - len(body) - 1) + "\n"
    assert len(padded.encode()) == MAX_MANIFEST_BYTES
    (tmp_path / MANIFEST_FILE_NAME).write_text(padded, newline="\n")
    assert load_manifest(tmp_path).entrypoint == ("python", "main.py")
    (tmp_path / MANIFEST_FILE_NAME).write_text(padded + "#", newline="\n")
    with pytest.raises(ManifestError, match="larger than"):
        load_manifest(tmp_path)


def test_defaults_are_filled_in() -> None:
    manifest = _parse()
    assert manifest.name == "job"
    assert manifest.env.base == "python-cpu"
    assert manifest.resources.gpu == "none"
    assert manifest.limits.timeout == DEFAULT_TIMEOUT_S
    assert manifest.limits.network == "default"
    assert manifest.outputs == ()


# ------------------------------------------------------------ typed errors


def test_unknown_key_reports_its_yaml_path() -> None:
    with pytest.raises(ManifestError) as info:
        _parse(resources={"gpus": 1})
    assert ("resources.gpus", "unknown key") in info.value.problems


def test_unknown_top_level_key_and_list_index_paths() -> None:
    with pytest.raises(ManifestError) as info:
        _parse(colour="blue", outputs=["ok/", "../escape"])
    paths = _paths(info)
    assert "colour" in paths
    assert "outputs" in paths


@pytest.mark.parametrize(("key", "version"), [("split", "V0.10"), ("inputs", "V0.5")])
def test_split_and_inputs_are_recognised_and_refused(key: str, version: str) -> None:
    with pytest.raises(ManifestError) as info:
        _parse(**{key: {"over": "data/*"}})
    assert _paths(info) == [key]
    assert version in info.value.message


def test_refused_future_key_also_through_model_validate() -> None:
    with pytest.raises(ValueError, match=r"V0\.10"):
        Manifest.model_validate({**BASE, "name": "x", "split": {}})


@pytest.mark.parametrize(
    "value",
    ["/abs/path", "C:/win", "a/../b", "..", "a\\b", " lead", "", "a\x07b"],
)
def test_outputs_must_stay_inside_the_project(value: str) -> None:
    with pytest.raises(ManifestError) as info:
        _parse(outputs=[value])
    assert _paths(info)[0].startswith("outputs")


def test_outputs_accept_globs_and_a_single_string() -> None:
    assert _parse(outputs="results/").outputs == ("results/",)
    assert _parse(outputs=["*.csv", "logs/**/x.txt"]).outputs == (
        "*.csv",
        "logs/**/x.txt",
    )


def test_error_messages_never_echo_the_value() -> None:
    secret = "s3cr3t-value-" + "z" * 50
    with pytest.raises(ManifestError) as info:
        _parse(env={"base": secret})
    assert secret not in info.value.message
    assert secret not in json.dumps(info.value.details)


def test_vram_without_gpu_is_refused() -> None:
    with pytest.raises(ManifestError) as info:
        _parse(resources={"vram_gb": 4})
    assert _paths(info) == ["resources"]


@pytest.mark.parametrize("value", ["3.12", "3.9"])
def test_python_version_shape(value: str) -> None:
    assert _parse(env={"python": value}).env.python == value


@pytest.mark.parametrize("value", [3.12, "3", "python3.12", "3.12.1", 3])
def test_python_version_refused(value: object) -> None:
    with pytest.raises(ManifestError) as info:
        _parse(env={"python": value})
    assert _paths(info) == ["env.python"]


def test_unquoted_python_version_from_yaml_gets_a_hint() -> None:
    with pytest.raises(ManifestError, match="quote"):
        parse_manifest_text("entrypoint: python main.py\nenv:\n  python: 3.12\n")


# ------------------------------------------------------------------ durations


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90s", 90),
        ("30m", 1800),
        ("2h", 7200),
        ("1h30m", 5400),
        ("1d", 86400),
        ("600", 600),
        (45, 45),
        ("2H", 7200),
    ],
)
def test_durations(text: object, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "h", "1x", "-5", "1.5h", True, 1.5, "1h 30m"])
def test_bad_durations(text: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


# ------------------------------------------------------------------ entrypoint


@pytest.mark.parametrize(
    ("text", "argv"),
    [
        ("python train.py --epochs 3", ("python", "train.py", "--epochs", "3")),
        ('python -c "print(1)"', ("python", "-c", "print(1)")),
        ("python 'a b.py'", ("python", "a b.py")),
        (r"python a\ b.py", ("python", "a b.py")),
        ('python x.py ""', ("python", "x.py", "")),
        ("python x.py # not a comment", ("python", "x.py", "#", "not", "a", "comment")),
    ],
)
def test_entrypoint_string_is_split_with_posix_rules(
    text: str, argv: tuple[str, ...]
) -> None:
    assert _parse(entrypoint=text).entrypoint == argv


@pytest.mark.parametrize(
    "text",
    [
        "python a.py | tee out.txt",
        "python a.py; rm -rf /",
        "python a.py && echo $HOME",
        "python *.py > log",
        "python $(whoami) `id`",
    ],
)
def test_no_shell_ever_metacharacters_stay_literal_arguments(text: str) -> None:
    argv = _parse(entrypoint=text).entrypoint
    assert argv[0] == "python"
    assert argv not in {("sh", "-c"), ("bash", "-c")}
    assert "sh" not in argv[:1] and "-c" not in argv[:2]
    # Every token of the text is one literal argument: nothing was expanded.
    assert list(argv) == text.split()


def test_argv_list_is_kept_verbatim() -> None:
    argv = ["python", "x.py", "a b", "$HOME", "*"]
    assert _parse(entrypoint=argv).entrypoint == tuple(argv)


@pytest.mark.parametrize(
    "value",
    [
        'python "unterminated',
        "python trailing\\",
        "",
        "   ",
        [],
        [""],
        ["p", 3],
        ["p", "nul\x00"],
    ],
)
def test_bad_entrypoints(value: object) -> None:
    with pytest.raises(ManifestError) as info:
        _parse(entrypoint=value)
    assert _paths(info) == ["entrypoint"]


# ------------------------------------------------------------------- inference


def test_inference_prefers_main_py(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    (tmp_path / "__main__.py").write_text("")
    assert load_manifest(tmp_path).entrypoint == ("python", "main.py")


def test_inference_falls_back_to_dunder_main(tmp_path: Path) -> None:
    (tmp_path / "__main__.py").write_text("")
    assert load_manifest(tmp_path).entrypoint == ("python", "__main__.py")


def test_inference_failure_lists_what_was_looked_for(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("")
    with pytest.raises(ManifestError) as info:
        load_manifest(tmp_path)
    assert _paths(info) == ["entrypoint"]
    assert "main.py" in info.value.message and "__main__.py" in info.value.message


def test_manifest_without_entrypoint_is_inferred_too(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    (tmp_path / MANIFEST_FILE_NAME).write_text("name: demo\n")
    manifest = load_manifest(tmp_path)
    assert manifest.name == "demo"
    assert manifest.entrypoint == ("python", "main.py")


def test_controller_side_parse_needs_an_entrypoint() -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest_data({"name": "x"})
    assert _paths(info) == ["entrypoint"]


def test_name_defaults_to_the_sanitised_folder_name(tmp_path: Path) -> None:
    project = tmp_path / "My Project!"
    project.mkdir()
    (project / "main.py").write_text("")
    assert load_manifest(project).name == "My-Project"
    assert sanitize_name("!!!") == "job"


def test_requirements_must_exist_inside_the_tree(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    (tmp_path / MANIFEST_FILE_NAME).write_text("env:\n  requirements: req.txt\n")
    with pytest.raises(ManifestError) as info:
        load_manifest(tmp_path)
    assert _paths(info) == ["env.requirements"]
    (tmp_path / "req.txt").write_text("numpy\n")
    assert load_manifest(tmp_path).env.requirements == "req.txt"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges")
def test_requirements_symlink_escaping_the_tree_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    (project / "main.py").write_text("")
    os.symlink(tmp_path / "outside.txt", project / "req.txt")
    (project / MANIFEST_FILE_NAME).write_text("env:\n  requirements: req.txt\n")
    with pytest.raises(ManifestError, match="outside"):
        load_manifest(project)


# ------------------------------------------------------------------------- YAML


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a: &x [1]\nb: *x\n", "aliases"),
        ("entrypoint: a\nentrypoint: b\n", "duplicate"),
        ("entrypoint: !!python/object/apply:os.system ['id']\n", "YAML"),
        ("- just\n- a list\n", "mapping"),
        ("entrypoint: [unclosed\n", "YAML"),
        ("[" * 5000, "YAML"),
    ],
)
def test_hostile_yaml_is_refused(text: str, fragment: str) -> None:
    with pytest.raises(ManifestError, match=fragment):
        parse_manifest_text(text)


def test_non_utf8_and_bom(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILE_NAME).write_bytes(b"entrypoint: python \xff.py\n")
    with pytest.raises(ManifestError, match="UTF-8"):
        load_manifest(tmp_path)
    (tmp_path / MANIFEST_FILE_NAME).write_bytes(
        b"\xef\xbb\xbfentrypoint: python a.py\n"
    )
    assert load_manifest(tmp_path).entrypoint == ("python", "a.py")


def test_empty_manifest_file_means_all_defaults(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    (tmp_path / MANIFEST_FILE_NAME).write_text("")
    assert load_manifest(tmp_path).entrypoint == ("python", "main.py")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges")
def test_symlinked_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real.yaml").write_text("entrypoint: python a.py\n")
    os.symlink(tmp_path / "real.yaml", tmp_path / MANIFEST_FILE_NAME)
    with pytest.raises(ManifestError, match="regular file"):
        load_manifest(tmp_path)


# -------------------------------------------------------------------- canonical


def test_canonical_json_is_order_independent_and_round_trips() -> None:
    a = parse_manifest_text(
        "entrypoint: python main.py\nname: demo\nlimits:\n  timeout: 2h\n"
        "resources:\n  gpu: preferred\n"
    )
    b = parse_manifest_text(
        "resources: {gpu: preferred}\nlimits: {timeout: 7200}\nname: demo\n"
        "entrypoint: [python, main.py]\n"
    )
    assert a.canonical_json() == b.canonical_json()
    again = parse_manifest_data(json.loads(a.canonical_json()))
    assert again == a
    assert again.canonical_json() == a.canonical_json()


def test_canonical_json_is_compact_and_sorted() -> None:
    text = _parse(name="z").canonical_json()
    assert " " not in text.replace('"python main.py"', "")
    keys = list(json.loads(text))
    assert keys == sorted(keys)
