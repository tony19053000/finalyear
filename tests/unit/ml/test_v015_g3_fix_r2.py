"""V0.15 G3 fix round 2 (tester r2 N3, reviewer #4) - pure parts.

* N3: a lone surrogate (U+D800-U+DFFF; not encodable as UTF-8) is replaced
  like a control character, valid astral characters are kept;
* reviewer #4: the ``fit`` child's output that cannot be read is a typed
  :class:`ChildOutputError` (-> ``child_output_invalid``), never a crash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshforge.controller.ml.registry import safe_json, safe_text
from meshforge.controller.ml_retrain_task import ChildOutputError, _read_output

pytestmark = pytest.mark.unit


def test_lone_surrogates_are_replaced_and_astral_characters_kept() -> None:
    assert safe_text("a\ud800b\udfffc") == "a�b�c"
    assert safe_text("smile \U0001f600 日本") == "smile \U0001f600 日本"
    out = safe_json({"k\udc00": ["v\ud800", {"x": "\udbff"}]})
    assert out == {"k�": ["v�", {"x": "�"}]}
    json.dumps(out).encode("utf-8")  # encodable now
    "".join(out).encode("utf-8")


def _write(out: Path, doc: object, files: dict[str, bytes] | None = None) -> None:
    out.mkdir()
    (out / "set.json").write_text(
        doc if isinstance(doc, str) else json.dumps(doc), "utf-8"
    )
    for name, data in (files or {}).items():
        (out / name).write_bytes(data)


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ("{not json", "JSONDecodeError"),
        ("[1, 2]", "not an object"),
        ({"members": [{"artefact": "a.art"}]}, "missing field 'version'"),
        ({"members": [{"artefact": "gone.art", "version": "v"}]}, "FileNotFound"),
        ({"members": [7]}, "AttributeError"),
    ],
)
def test_unreadable_child_output_is_typed(
    tmp_path: Path, doc: object, needle: str
) -> None:
    out = tmp_path / "out"
    _write(out, doc)
    with pytest.raises(ChildOutputError) as caught:
        _read_output(out)
    assert needle in str(caught.value)


def test_missing_set_json_and_bad_timings_are_typed(tmp_path: Path) -> None:
    with pytest.raises(ChildOutputError):
        _read_output(tmp_path / "nothing")
    out = tmp_path / "out"
    _write(out, {"members": []})
    (out / "timings.json").write_text("[]", "utf-8")
    with pytest.raises(ChildOutputError, match="timings"):
        _read_output(out)


def test_good_child_output_reads(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _write(
        out,
        {"members": [{"artefact": "a.art", "version": "v"}, {"version": "n"}]},
        {"a.art": b"bytes"},
    )
    (out / "timings.json").write_text('{"total_s": 1.0}', "utf-8")
    doc, artefacts, timings = _read_output(out)
    assert artefacts == {"v": b"bytes"}
    assert timings == {"total_s": 1.0}
    assert len(doc["members"]) == 2
