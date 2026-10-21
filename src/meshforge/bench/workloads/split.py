"""Mode 3 benchmark families (V0.10 G8, roadmap 0.10.9, D272).

Four families whose job is a **split job** (``split:`` manifest, D260): its
items are resolved at submit, its lanes run :mod:`.kernels.split` once per
chunk, a built-in combine joins the parts. They cover the three bottlenecks
the planner must learn to tell apart, and every built-in combine:

==========================  ===========  ==============  ================
family                      over         bound by        combine
==========================  ===========  ==============  ================
``split.image_classify``    ``glob``     CPU (CNN)       ``concat``
``split.hash_files``        ``glob``     bytes moved     ``concat``
``split.montecarlo_range``  ``range``    CPU (ideal)     ``merge-json``
``split.csv_aggregate``     ``lines``    CPU per row     ``merge-csv``
==========================  ===========  ==============  ================

``split.image_classify`` is criterion 1's workload (D274): its CNN runs in
**integer** arithmetic, so its output is identical on every laptop (the
brief's "CPU-deterministic mode", §7 Q3); a GPU variant is a follow-up.
``split.hash_files`` is the "don't split on slow Wi-Fi" lesson: little work
per byte, so shipping the files dominates.

Each family has the V0.9 contract (:class:`~.base.Family`: params model,
size params, ``expected_runtime_s`` hint for the **whole job on one node**,
the 5 s - 3 min window) plus what a split family adds
(:class:`SplitFamily`): the job's ``split:`` spec, its generated input data
(``data/``, from the seed - nothing downloaded) and :meth:`SplitFamily.verify_output`,
the **whole-output** self-check (every item once, in order, each value
recomputed). Inside the fabric each chunk checks itself
(:mod:`.kernels.split`); a run whose job SUCCEEDED therefore passed every
chunk's check.

Hints were calibrated on Laptop 1 (``docs/BENCHMARKS.md`` "Mode 3 families");
they are single-thread numbers (the kernels are numpy integer / float loops
and ``hashlib`` / ``zlib``, one core per chunk process).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from pydantic import Field

from .base import (
    Family,
    Kind,
    Metric,
    Params,
    Verdict,
    WorkloadResult,
    calibrated,
    register,
)
from .kernels import split as kernel
from .kernels.runner import RESULT_SCHEMA_VERSION

__all__ = [
    "MAX_INPUT_BYTES",
    "CsvAggregateFamily",
    "HashFilesFamily",
    "ImageClassifyFamily",
    "MontecarloRangeFamily",
    "SplitFamily",
]

MAX_INPUT_BYTES: Final = 64 * 1024 * 1024
"""Most bytes of generated input one split job carries (built in memory on the
controller at submit; a larger point is refused by ``combination_problems``)."""
_METRIC: Final[Metric] = "cpu_fp32_gflops_1t"


class SplitFamily[P: Params](Family[P]):
    """A Mode 3 family (module docstring)."""

    split: ClassVar[bool] = True
    kind: ClassVar[Kind] = Kind.CPU
    combine: ClassVar[str]
    """The built-in combine (``concat`` / ``merge-csv`` / ``merge-json``)."""
    output_file: ClassVar[str]
    """The one file each chunk writes (the combine joins it)."""

    @property
    def gpu(self) -> bool:
        return False

    def metric(self, params: P) -> Metric:
        return _METRIC

    # ------------------------------------------------------------ the job

    def over(self, params: P) -> dict[str, Any]:
        """The ``split.over`` of the job."""
        raise NotImplementedError

    def split_spec(self, params: P | Mapping[str, Any]) -> dict[str, Any]:
        """The job's ``split:`` (auto chunks: the planner sizes them)."""
        p = self._model(params)
        return {"over": self.over(p), "chunk": "auto", "combine": self.combine}

    def input_files(self, params: P | Mapping[str, Any], seed: int) -> dict[str, bytes]:
        """The job's generated data (relative path -> bytes)."""
        del params, seed
        return {}

    def input_bytes(self, params: P) -> int:
        """Bytes :meth:`input_files` makes (no generation)."""
        del params
        return 0

    def combination_problems(self, params: P) -> list[str]:
        size = self.input_bytes(params)
        if size > MAX_INPUT_BYTES:
            return [
                f"{', '.join(self.size_params)}: the input data would be {size} "
                f"bytes, more than {MAX_INPUT_BYTES}"
            ]
        return []

    # ------------------------------------------------------------ in-process

    def items_file(self, params: P | Mapping[str, Any], seed: int) -> bytes:
        """The items file of a one-chunk run of every item (what a lane's
        ``{items_file}`` holds for "don't split")."""
        raise NotImplementedError

    def expected_output(
        self, params: P | Mapping[str, Any], seed: int, files: Mapping[str, bytes]
    ) -> dict[str, bytes]:
        """The combined output of every item, computed in-process from the
        job's input ``files`` (relative path -> bytes)."""
        p = self.params_dict(params)
        return _in_tree(files, lambda: self._run_all(p, seed, files))

    def _run_all(
        self, params: Mapping[str, Any], seed: int, files: Mapping[str, bytes]
    ) -> dict[str, bytes]:
        out, problem, _ns = kernel.run_items(
            self.name, params, seed, self.items_file(params, seed)
        )
        if problem is not None:
            raise AssertionError(f"{self.name}: in-process self-check: {problem}")
        return out

    def verify_output(
        self,
        files: Mapping[str, bytes],
        params: P | Mapping[str, Any],
        seed: int,
    ) -> Verdict:
        """The whole-output self-check of a combined output ``files`` (output
        relative path -> bytes): exactly the expected files, every item once
        and in order, each value recomputed from the seed."""
        p = self._model(params)
        expected = self.expected_output(p, seed, self.input_files(p, seed))
        problems: list[str] = []
        if sorted(files) != sorted(expected):
            problems.append(f"files {sorted(files)}, expected {sorted(expected)}")
        for name, want in expected.items():
            got = files.get(name)
            if got is None:
                continue
            if not self._same(name, got, want):
                problems.append(f"{name}: differs from the recomputed output")
        return Verdict(ok=not problems, problems=tuple(problems))

    def _same(self, name: str, got: bytes, want: bytes) -> bool:
        del name
        return got == want

    def run(
        self,
        params: P | Mapping[str, Any],
        seed: int,
        *,
        device: str | None = None,
    ) -> WorkloadResult:
        """Every item as one chunk, in-process (the job's "don't split" run);
        the result carries the output's checksum and the chunk's check."""
        del device
        self.check_seed(seed)
        p = self.params_dict(self.parse_params(params))
        files = self.input_files(p, seed)

        def go() -> tuple[dict[str, bytes], str | None, int]:
            return kernel.run_items(self.name, p, seed, self.items_file(p, seed))

        out, problem, compute_ns = _in_tree(files, go)
        checksum = _files_digest(out)
        return WorkloadResult(
            schema_version=RESULT_SCHEMA_VERSION,
            family=self.name,
            params=p,
            seed=seed,
            status="ok" if problem is None else "check_failed",
            reason=problem,
            device="cpu",
            compute_ns=max(1, compute_ns),
            compute_iterations=1,
            checksum=checksum,
            checksum_exact=True,
            verified=problem is None,
            check_detail=problem or f"{len(out)} output file(s) checked",
        )

    def verify(self, result: WorkloadResult) -> Verdict:
        problems: list[str] = []
        if result.family != self.name:
            problems.append(f"family {result.family!r} is not {self.name!r}")
        if result.status != "ok" or not result.verified:
            problems.append(f"status {result.status}: {result.reason or ''}".strip())
        if not (result.checksum or "").startswith("sha256:"):
            problems.append("no checksum")
        return Verdict(ok=not problems, problems=tuple(problems))

    def describe(self) -> dict[str, Any]:
        out = super().describe()
        out["split"] = {"combine": self.combine, "output": self.output_file}
        return out


def _files_digest(files: Mapping[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for name in sorted(files):
        for part in (name.encode(), files[name]):
            hasher.update(len(part).to_bytes(8, "little"))
            hasher.update(part)
    return "sha256:" + hasher.hexdigest()


def _in_tree[T](files: Mapping[str, bytes], go: Callable[[], T]) -> T:
    """Run ``go()`` with the job's input files present at their relative
    paths (the kernels read items by path, as in a lane's workdir): a private
    temporary folder is the working directory meanwhile. Changes the
    process's working directory - for tests and local calibration only (the
    fabric runs the kernel in the lane's own process); serialised by a lock."""
    with _CWD_LOCK, tempfile.TemporaryDirectory(prefix="meshforge-split-") as root:
        for rel, data in files.items():
            target = Path(root).joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        before = os.getcwd()
        os.chdir(root)
        try:
            return go()
        finally:
            os.chdir(before)


_CWD_LOCK: Final = threading.Lock()


# ------------------------------------------------------------ image_classify


class ImageClassifyParams(Params):
    images: int = Field(3000, ge=10, le=20_000, description="synthetic images")
    size: Literal[16, 32] = Field(32, description="image side (pixels)")
    width: int = Field(32, ge=4, le=32, description="CNN channels")


class ImageClassifyFamily(SplitFamily[ImageClassifyParams]):
    name = "split.image_classify"
    description = (
        "classify synthetic images with a small integer CNN (glob over the "
        "images; concat) - criterion 1's workload, identical on every laptop"
    )
    params_model = ImageClassifyParams
    size_params = ("images", "size", "width")
    combine = "concat"
    output_file = "predictions.csv"
    needs_numpy = True

    # integer matmul MACs per second on Laptop 1, one core (calibrated)
    _MACS_PER_S: ClassVar = 1.25e9

    def macs(self, params: ImageClassifyParams) -> float:
        s, w = params.size, params.width
        half = s // 2
        return s * s * 27 * w + half * half * 9 * w * w + w * 10

    def work_units(self, params: ImageClassifyParams) -> float:
        return params.images * self.macs(params)

    def efficiency(self, params: ImageClassifyParams) -> float:
        return calibrated(self._MACS_PER_S, _METRIC)

    def ram_gb(self, params: ImageClassifyParams) -> float:
        return 0.5

    def over(self, params: ImageClassifyParams) -> dict[str, Any]:
        return {"glob": f"{kernel.IMAGE_DIR}/*.ppm"}

    def input_bytes(self, params: ImageClassifyParams) -> int:
        header = len(f"P6\n{params.size} {params.size}\n255\n")
        return params.images * (header + params.size * params.size * 3)

    def input_files(
        self, params: ImageClassifyParams | Mapping[str, Any], seed: int
    ) -> dict[str, bytes]:
        p = self._model(params)
        return {
            kernel.image_name(i): kernel.image_bytes(seed, i, p.size)
            for i in range(p.images)
        }

    def items_file(
        self, params: ImageClassifyParams | Mapping[str, Any], seed: int
    ) -> bytes:
        p = self._model(params)
        return json.dumps([kernel.image_name(i) for i in range(p.images)]).encode()


# ------------------------------------------------------------ hash_files


class HashFilesParams(Params):
    files: int = Field(256, ge=4, le=4096, description="files to hash")
    size_kb: int = Field(256, ge=4, le=1024, description="bytes per file (KiB)")
    rounds: int = Field(100, ge=1, le=256, description="chained SHA-256 passes")


class HashFilesFamily(SplitFamily[HashFilesParams]):
    name = "split.hash_files"
    description = (
        "chained SHA-256 + zlib over files (glob over the files; concat) - "
        "little work per byte: shipping the files can cost more than it saves"
    )
    params_model = HashFilesParams
    size_params = ("files", "size_kb", "rounds")
    combine = "concat"
    output_file = "hashes.csv"

    # Laptop 1, one core: sha256 ~1.9 GB/s, zlib level 6 on half-random data
    # ~ 0.13 GB/s (bytes hashed per second x rounds + bytes compressed)
    _SHA_S_PER_BYTE: ClassVar = 1 / 1.9e9
    _ZLIB_S_PER_BYTE: ClassVar = 1 / 0.13e9
    _UNIT_S: ClassVar = 1e-9

    def work_units(self, params: HashFilesParams) -> float:
        size = params.files * params.size_kb * 1024
        seconds = size * (params.rounds * self._SHA_S_PER_BYTE + self._ZLIB_S_PER_BYTE)
        return seconds / self._UNIT_S

    def efficiency(self, params: HashFilesParams) -> float:
        return calibrated(1.0 / self._UNIT_S, _METRIC)

    def over(self, params: HashFilesParams) -> dict[str, Any]:
        return {"glob": f"{kernel.FILE_DIR}/*.bin"}

    def input_bytes(self, params: HashFilesParams) -> int:
        return params.files * params.size_kb * 1024

    def input_files(
        self, params: HashFilesParams | Mapping[str, Any], seed: int
    ) -> dict[str, bytes]:
        p = self._model(params)
        return {
            kernel.file_name(i): kernel.file_bytes(seed, i, p.size_kb * 1024)
            for i in range(p.files)
        }

    def items_file(
        self, params: HashFilesParams | Mapping[str, Any], seed: int
    ) -> bytes:
        p = self._model(params)
        return json.dumps([kernel.file_name(i) for i in range(p.files)]).encode()


# ------------------------------------------------------------ montecarlo_range


class MontecarloRangeParams(Params):
    items: int = Field(3000, ge=16, le=100_000, description="range items")
    samples: int = Field(
        200_000, ge=10_000, le=5_000_000, description="points per item"
    )


class MontecarloRangeFamily(SplitFamily[MontecarloRangeParams]):
    name = "split.montecarlo_range"
    description = (
        "estimate pi/4 from points drawn per item, each item its own seeded "
        "stream (range items; merge-json) - pure CPU, ideal scaling"
    )
    params_model = MontecarloRangeParams
    size_params = ("items", "samples")
    combine = "merge-json"
    output_file = "results.json"
    needs_numpy = True

    # numpy PCG64 points (x, y) tested per second on Laptop 1, one core
    _POINTS_PER_S: ClassVar = 6.9e7

    def work_units(self, params: MontecarloRangeParams) -> float:
        return float(params.items) * params.samples

    def efficiency(self, params: MontecarloRangeParams) -> float:
        return calibrated(self._POINTS_PER_S, _METRIC)

    def over(self, params: MontecarloRangeParams) -> dict[str, Any]:
        return {"range": {"start": 0, "stop": params.items, "step": 1}}

    def items_file(
        self, params: MontecarloRangeParams | Mapping[str, Any], seed: int
    ) -> bytes:
        p = self._model(params)
        return json.dumps(list(range(p.items))).encode()

    def _same(self, name: str, got: bytes, want: bytes) -> bool:
        """merge-json writes its own compact form: compare the values."""
        try:
            return bool(json.loads(got) == json.loads(want))
        except ValueError:
            return False


# ------------------------------------------------------------ csv_aggregate


class CsvAggregateParams(Params):
    rows: int = Field(20_000, ge=100, le=100_000, description="table records")
    cols: int = Field(16, ge=2, le=32, description="integer columns per record")
    rounds: int = Field(1500, ge=1, le=20_000, description="SHA-256 chain per row")


class CsvAggregateFamily(SplitFamily[CsvAggregateParams]):
    name = "split.csv_aggregate"
    description = (
        "per-record sum / min / max + a SHA-256 chain over a CSV table (lines "
        "with a header; merge-csv)"
    )
    params_model = CsvAggregateParams
    size_params = ("rows", "cols", "rounds")
    combine = "merge-csv"
    output_file = "aggregates.csv"

    # Laptop 1, one core: SHA-256 of 32 bytes ~5.2 M/s; parsing a value
    # ~ 0.15 us
    _HASH_S: ClassVar = 1 / 5.2e6
    _VALUE_S: ClassVar = 0.15e-6
    _UNIT_S: ClassVar = 1e-9

    def work_units(self, params: CsvAggregateParams) -> float:
        seconds = params.rows * (
            params.rounds * self._HASH_S + params.cols * self._VALUE_S
        )
        return seconds / self._UNIT_S

    def efficiency(self, params: CsvAggregateParams) -> float:
        return calibrated(1.0 / self._UNIT_S, _METRIC)

    def over(self, params: CsvAggregateParams) -> dict[str, Any]:
        return {"lines": kernel.TABLE_FILE, "header": True}

    def input_bytes(self, params: CsvAggregateParams) -> int:
        return params.rows * (7 + 6 * params.cols) + 6 * params.cols

    def input_files(
        self, params: CsvAggregateParams | Mapping[str, Any], seed: int
    ) -> dict[str, bytes]:
        p = self._model(params)
        return {kernel.TABLE_FILE: kernel.table_bytes(seed, p.rows, p.cols)}

    def items_file(
        self, params: CsvAggregateParams | Mapping[str, Any], seed: int
    ) -> bytes:
        p = self._model(params)
        return kernel.table_bytes(seed, p.rows, p.cols)


SPLIT_FAMILIES: Final = (
    ImageClassifyFamily(),
    HashFilesFamily(),
    MontecarloRangeFamily(),
    CsvAggregateFamily(),
)

for _family in SPLIT_FAMILIES:
    register(_family)
