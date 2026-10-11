"""V0.7 G4 tester round 2, T-3: names that normalise or truncate into a real user.

``LoginService`` keys on ``name.strip().lower()[:MAX]``; Python's ``lower()`` maps
some non-ASCII code points (KELVIN SIGN U+212A) to ASCII letters. Whatever the
outcome, the answer must be a clean 4xx or 200 for the *same* user - never a 5xx,
and one spelling must not dodge the failure counter of another.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY
from mf_users import create_user
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

KELVIN = chr(0x212A)  # lower() -> "k"
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]
PW = "kevin-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_free_failures": 3,
            "login_backoff_base_s": 30.0,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
        },
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def anon(
    runtime: ControllerRuntime, engine: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    await create_user(engine, "kevin", PW)
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        yield client


async def _login(c: httpx.AsyncClient, name: str, pw: str) -> httpx.Response:
    return await c.post("/v1/auth/login", json={"name": name, "password": pw})


@pytest.mark.parametrize(
    "name",
    [
        KELVIN + "evin",  # KELVIN SIGN lower() -> 'k'
        "KEVIN",
        "  kevin  ",
        "kevin" + "x" * 5000,
        chr(0x130) + "kevin",  # dotted capital I
        "kev\u200bin",  # zero width space
        "kevin" + chr(0x2028),
        "kevin" + chr(0xFEFF),
        "k" * 64 + "\x00",
        "\U0001f600" * 40,
        chr(0x202E) + "kevin",  # RTL override
    ],
)
async def test_odd_spellings_never_5xx_and_never_log_in_a_different_user(
    anon: httpx.AsyncClient, name: str
) -> None:
    r = await _login(anon, name, PW)
    assert r.status_code < 500, (r.status_code, r.text)
    wrong = await _login(anon, name, "definitely-wrong-pw")
    assert wrong.status_code in (400, 401, 429), (wrong.status_code, wrong.text)
    if r.status_code == 200:
        # Allowed only when it is plainly the same user (case / padding).
        assert name.strip().lower() == "kevin", f"{name!r} logged in as kevin"


@pytest.mark.xfail(
    strict=True,
    reason="MINOR FINDING: a KELVIN SIGN spelling normalises to 'kevin' and "
    "authenticates; harmless without the password, but an unexpected alias.",
)
async def test_a_non_ascii_look_alike_is_not_an_alias_of_the_ascii_name(
    anon: httpx.AsyncClient,
) -> None:
    assert (await _login(anon, KELVIN + "evin", PW)).status_code != 200


async def test_spellings_share_one_failure_counter_so_none_dodges_backoff(
    anon: httpx.AsyncClient,
) -> None:
    for spelling in ("kevin", "KEVIN", " kevin "):
        assert (await _login(anon, spelling, "wrong-wrong-1")).status_code == 401
    # the 4th failure (another spelling) is past the free ones: it earns backoff
    assert (await _login(anon, "Kevin", "wrong-wrong-1")).status_code in (401, 429)
    # 3 free failures are spent: the correct password is now delayed/refused for
    # EVERY spelling (429 with Retry-After), not accepted through a fresh one.
    for spelling in ("kevin", "KEVIN", KELVIN + "evin"):
        r = await _login(anon, spelling, PW)
        assert r.status_code == 429, (spelling, r.status_code, r.text)
