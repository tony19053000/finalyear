"""Dashboard e2e fixtures (V0.8 G2, D179): a real controller process serving
the built SPA, driven by real Chromium through pytest-playwright.

* PostgreSQL: the component tier's fixtures (``tests/_support/mf_postgres.py``)
  - one container per session, a fresh database per test.
* Controller: the cluster harness :class:`mf_cluster.Fabric` (real
  ``meshforge-controller`` process, HTTPS with the test fabric CA, users
  ``admin`` and ``alice`` created with the real CLI) plus one real agent, so
  the header's node count has a real source. Torn down with the harness's
  invariant check (no tracebacks, no forbidden events).
* The SPA: the controller serves ``src/meshforge/controller/web/dist`` - the
  output of ``cd frontend && npm run build``. Not built -> skip with that
  command as the reason (as are a missing Playwright Chromium and a missing
  ``frontend/node_modules`` for axe-core).
* Browser trust: Chromium accepts exactly this controller's TLS key
  (``--ignore-certificate-errors-spki-list``, pinned after the harness
  verified the certificate against the fabric CA) - not any certificate.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mf_postgres import pg_settings, postgres_server  # noqa: F401

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from playwright.sync_api import Browser, BrowserType, Playwright

    from meshforge.controller.store.settings import DatabaseSettings


@pytest.fixture(scope="session")
def dashboard_dist() -> Path:
    from mf_dashboard import AXE_JS, DIST

    if not (DIST / "index.html").is_file():
        pytest.skip(
            "the dashboard is not built - run `cd frontend && npm ci && npm run build`"
        )
    if not AXE_JS.is_file():
        pytest.skip(
            "frontend/node_modules is missing (axe-core) - run `cd frontend && npm ci`"
        )
    return DIST


@pytest.fixture(scope="module")
def playwright() -> Iterator[Playwright]:
    """pytest-playwright's ``playwright`` fixture, narrowed to module scope.

    The sync API keeps an event loop running in the main thread while it is
    started; with the plugin's session scope it stayed up after the e2e tests
    and every later ``asyncio`` test in the same run failed (fix round 1).
    Stopped at the end of each e2e module, the loop never leaks out of this
    directory. (Tracing via ``--tracing`` is not wired here.)
    """
    from playwright.sync_api import sync_playwright

    started = sync_playwright().start()
    try:
        yield started
    finally:
        started.stop()


@pytest.fixture(scope="module")
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    return getattr(playwright, browser_name)  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def chromium_installed(browser_type: BrowserType) -> None:
    if not Path(browser_type.executable_path).exists():
        pytest.skip(
            "Playwright's Chromium is not installed - run `uv run playwright install chromium`"
        )


@pytest.fixture(autouse=True)
def _trust_the_test_fabric(monkeypatch: pytest.MonkeyPatch) -> None:
    import mf_fabric

    monkeypatch.setenv("SSL_CERT_FILE", str(mf_fabric.ca_file()))


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    """Extra :class:`mf_cluster.Fabric` arguments; a test overrides it with
    ``@pytest.mark.parametrize("fabric_options", [{...}])``. A controller
    restart needs ``record_links=True``: the REST port then sits behind a
    stable proxy address, as a real controller keeps its configured port (the
    harness otherwise restarts it on a new ephemeral port, which no browser
    page could follow). ``agents`` (default 1) sets how many real agents run."""
    return {}


@pytest.fixture
def fabric(
    pg_settings: DatabaseSettings,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    dashboard_dist: Path,
    fabric_options: dict[str, Any],
) -> Iterator[Fabric]:
    pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
    pytest.importorskip(
        "sqlalchemy", reason="the controller needs the controller extra"
    )
    from mf_cluster import Fabric

    options = dict(fabric_options)
    agents = int(options.pop("agents", 1))  # V0.8 G3: pages with >= 2 nodes
    made = Fabric(pg_settings, tmp_path / "fabric", agents=agents, **options)
    try:
        made.start()
        yield made
        made.check_invariants()
    finally:
        made.close()


@pytest.fixture
def dashboard_browser(
    fabric: Fabric, browser_type: BrowserType, chromium_installed: None
) -> Iterator[Browser]:
    """Chromium that trusts exactly ``fabric``'s controller key."""
    from urllib.parse import urlsplit

    from mf_dashboard import spki_pin

    assert fabric.ssl_context is not None
    parts = urlsplit(fabric.http_base)
    assert parts.hostname is not None and parts.port is not None
    pin = spki_pin(parts.hostname, parts.port, fabric.ssl_context)
    browser = browser_type.launch(args=[f"--ignore-certificate-errors-spki-list={pin}"])
    try:
        yield browser
    finally:
        browser.close()
