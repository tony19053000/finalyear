"""V1.0 G7 (D423): the INSTALLED controller can train the ML planner's models.

Before D423 the controller bundle had the ``controller`` extra only, so on a
packaged controller every retrain and ``compute planner ml compare`` exited 2
("install the controller's ml extra") - and the L4 lab day trains on exactly that
controller. Here the real ``meshforge-controller`` .deb is installed in a clean
Ubuntu 24.04 systemd container (no Python on the machine) and, as the service
user, its own interpreter

1. imports the whole ML stack (scikit-learn + scipy, LightGBM - whose OpenMP
   runtime comes from the declared ``libgomp1`` - XGBoost, pyarrow's Parquet
   reader) with ``missing_libraries() == []``;
2. runs the real retrain child, ``python -m meshforge.controller.ml.train fit``,
   on a small synthetic G1 dataset: exit 0, a ``set.json`` that names every
   library version, and at least one learned member artefact;
3. reports ``ml`` among the bundle's extras in ``BUILDINFO.json``.

Slow: builds the controller bundle and .deb unless CI passes them in with
``MESHFORGE_TEST_CONTROLLER_DEB_DIR`` (the package-linux job does).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("pyarrow", reason="writes the dataset: needs the controller extra")

from mf_deb import (
    CONTROLLER_UNIT,
    Debs,
    SystemdBox,
    built_controller_debs,
    systemd_image,
)
from mf_ml_synthetic import parquet_file, synthetic_rows

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

CTL: Final = "/opt/meshforge/controller/current"
CTL_PYTHON: Final = f"{CTL}/python/bin/python3.12"
SERVICE_USER: Final = "meshforge"
IMPORTS: Final = (
    "import sklearn, scipy, lightgbm, xgboost, pyarrow.parquet; "
    "from meshforge.controller.ml.train import missing_libraries; "
    "print(missing_libraries())"
)


@pytest.fixture(scope="module")
def controller_debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("controller-debs"))


@pytest.fixture
def installed(controller_debs: Debs) -> Iterator[SystemdBox]:
    box = SystemdBox.start(systemd_image("24.04"), name_hint="ctlml")
    try:
        done = box.install(controller_debs.first)
        assert done.returncode == 0, done.stdout[-4000:]
        box.wait_unit(CONTROLLER_UNIT, ["active"], within=120)
        yield box
    finally:
        box.remove()


def test_the_installed_controller_imports_the_ml_stack_and_trains(
    installed: SystemdBox, tmp_path: Path
) -> None:
    box = installed
    assert box.run("command -v python3 || true").stdout == ""  # its own runtime only

    out = box.ok(f"{CTL_PYTHON} -I -c '{IMPORTS}'", user=SERVICE_USER)
    assert out.strip() == "[]", out

    dataset = parquet_file(synthetic_rows("signal"), tmp_path / "dataset.parquet")
    box.copy_in(dataset, "/tmp/dataset.parquet")
    box.ok("chmod 0644 /tmp/dataset.parquet")
    done = box.run(
        f"{CTL_PYTHON} -I -m meshforge.controller.ml.train fit "
        "--dataset /tmp/dataset.parquet --out /tmp/ml-fit",
        user=SERVICE_USER,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-3000:]
    assert json.loads(done.stdout.strip().splitlines()[-1])["set"].endswith("set.json")

    doc = json.loads(box.ok("cat /tmp/ml-fit/set.json"))
    libraries = doc["libraries"]
    for name in ("scikit-learn", "scipy", "lightgbm", "xgboost", "numpy", "pyarrow"):
        assert libraries.get(name), (name, libraries)
    # every candidate algorithm was tried: none of them is missing in the bundle
    assert set(doc["settings"]["algorithms"]) >= {
        "ridge",
        "forest",
        "lightgbm",
        "xgboost",
    }
    learned = [m for m in doc["members"] if m["algorithm"] not in ("none", "raw")]
    assert learned, doc["members"]
    arts = box.ok("ls /tmp/ml-fit").split()
    assert any(name.endswith(".art") for name in arts), arts

    info = json.loads(box.ok(f"cat {CTL}/BUILDINFO.json"))
    assert "ml" in info["extras"] and "controller" in info["extras"], info["extras"]
