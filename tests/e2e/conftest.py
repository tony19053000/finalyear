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
  output of ``cd frontend && npm run build``. Not built, or built from other
  frontend sources than the ones on disk (stale, D206) -> skip with that
  command as the reason (as are a missing Playwright Chromium and a missing
  ``frontend/node_modules`` for axe-core).
* Browser trust: Chromium accepts exactly this controller's TLS key
  (``--ignore-certificate-errors-spki-list``, pinned after the harness
  verified the certificate against the fabric CA) - not any certificate.

V0.8 G7 (one shared layer):

* ``fabric`` runs **2 real agents** by default (``fabric_options`` may say
  ``agents``: 1 for a test about exactly one laptop).
* ``new_page`` is the one way a test opens a page. When the test passed, it
  checks EVERY page it opened (``PageWatch.final_problems``): no console error,
  no uncaught error, no CSP violation, and axe-core clean on the page's final
  state - so the guard cannot be forgotten per test. A test states what it
  expects with ``watch.allow`` / ``allow_matching`` / ``skip_final_axe`` /
  ``disarm`` (each with its reason). The shared helpers (sign-in, axe, Tab
  navigation, job API) are in ``tests/_support/mf_e2e.py``.
* ``leak_watch`` (one per session, checklist 25): every page from
  ``new_page`` reports its console, the URLs it requested and visited, and at
  the end of each test its storage (web storage, IndexedDB, script-readable
  cookies); credentials are learnt from the traffic
  (``tests/_support/mf_leaks.py``). ``test_v08_g7_zz_leak_gate.py`` runs last
  and asserts that no credential reached any of those places.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from mf_postgres import pg_settings, postgres_server  # noqa: F401

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_dashboard import PageWatch
    from mf_e2e import NewPage
    from mf_leaks import LeakWatch
    from playwright.sync_api import Browser, BrowserType, Page, Playwright

    from meshforge.controller.store.settings import DatabaseSettings

#: Agents the shared fabric runs unless a test's ``fabric_options`` says.
DEFAULT_AGENTS: Final = 2
#: Default Playwright timeout of a page from ``new_page`` (ms).
PAGE_TIMEOUT_MS: Final = 20_000

_CALL_PASSED: Final = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Remember whether the test body passed: the end-of-test page guard only
    judges a passing test (a failed one already reported its own problem)."""
    report: pytest.TestReport = yield
    if report.when == "call":
        item.stash[_CALL_PASSED] = report.passed
    return report


@pytest.fixture(scope="session")
def dashboard_dist() -> Path:
    from mf_dashboard import AXE_JS, DIST, require_fresh_dist

    # Not built, or built from other sources than these (V0.8 G7, D206).
    require_fresh_dist(DIST)
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
    page could follow). ``agents`` (default :data:`DEFAULT_AGENTS`) sets how
    many real agents run."""
    return {}


@pytest.fixture(scope="session")
def leak_watch() -> LeakWatch:
    """Where credentials could leak, across the whole e2e run (checklist 25)."""
    from mf_leaks import LeakWatch

    return LeakWatch()


@pytest.fixture
def fabric(
    pg_settings: DatabaseSettings,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    dashboard_dist: Path,
    fabric_options: dict[str, Any],
    leak_watch: LeakWatch,
) -> Iterator[Fabric]:
    pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
    pytest.importorskip(
        "sqlalchemy", reason="the controller needs the controller extra"
    )
    from mf_cluster import ADMIN_USER, NORMAL_USER, Fabric

    options = dict(fabric_options)
    agents = int(options.pop("agents", DEFAULT_AGENTS))
    made = Fabric(pg_settings, tmp_path / "fabric", agents=agents, **options)
    try:
        made.start()
        for user in (ADMIN_USER, NORMAL_USER):
            leak_watch.add_known(made.password(user))
            leak_watch.add_known(made.api_token(user))
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


@pytest.fixture
def new_page(
    dashboard_browser: Browser,
    request: pytest.FixtureRequest,
    leak_watch: LeakWatch,
) -> Iterator[NewPage]:
    """``new_page(init=None, timeout_ms=..., **context) -> (page, watch)``: a
    page in a fresh browser context (reduced motion: axe measures colours at
    rest), watched from its first document. At the end of a passing test every
    page made here must be clean (console, uncaught errors, CSP, axe on the
    final state); the test fails at teardown otherwise, listing each problem."""
    from mf_dashboard import PageWatch

    watches: list[PageWatch] = []

    def make(
        *, init: str | None = None, timeout_ms: int = PAGE_TIMEOUT_MS, **context: Any
    ) -> tuple[Page, PageWatch]:
        context.setdefault("reduced_motion", "reduce")
        page = dashboard_browser.new_context(**context).new_page()
        page.set_default_timeout(timeout_ms)
        if init:
            page.add_init_script(init)
        watch = PageWatch.attach(page)
        watches.append(watch)
        leak_watch.attach(page, test)
        return page, watch

    test = request.node.nodeid
    yield make
    for watch in watches:
        leak_watch.snapshot(watch.page, test)
    if not request.node.stash.get(_CALL_PASSED, False):
        return
    problems = [problem for watch in watches for problem in watch.final_problems()]
    assert not problems, "the end-of-test page guard found:\n  " + "\n  ".join(problems)
