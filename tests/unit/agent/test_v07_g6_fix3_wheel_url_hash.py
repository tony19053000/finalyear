"""V0.7 G6 fix round 3, T-18 (D164): on a wheels-only node a wheel URL must
carry ``--hash`` with a strong digest - refused typed otherwise, naming the
line. (Real uv, a redirect to plain http and a swapped file:
``tests/component/envs/test_v07_g6_fix3_wheel_url_real_uv.py``.)
"""

from __future__ import annotations

import pytest

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

URL = "https://files.example/pkg-1-py3-none-any.whl"
S256, S384, S512 = "ab" * 32, "cd" * 48, "ef" * 64

REFUSED = {
    "no-hash": f"pkg @ {URL}",
    "no-hash-glued": f"pkg@{URL}",
    "no-hash-bare-url": URL,
    "no-hash-userinfo": "pkg @ https://u:p@files.example/p-1-py3-none-any.whl",
    "fragment-is-not-a-pin": f"pkg @ {URL}#sha256={S256}",
    "md5": f"pkg @ {URL} --hash=md5:{'0' * 32}",
    "sha1": f"pkg @ {URL} --hash=sha1:{'0' * 40}",
    "sha256-too-short": f"pkg @ {URL} --hash=sha256:{'0' * 63}",
    "sha256-too-long": f"pkg @ {URL} --hash=sha256:{'0' * 65}",
    "sha256-not-hex": f"pkg @ {URL} --hash=sha256:{'g' * 64}",
    "empty-value": f"pkg @ {URL} --hash=",
    "algorithm-only": f"pkg @ {URL} --hash=sha256:",
    "no-algorithm": f"pkg @ {URL} --hash={S256}",
    "one-strong-one-weak": f"pkg @ {URL} --hash=sha256:{S256} --hash=md5:{'0' * 32}",
    "hash-of-another-line": f"pkg @ {URL}\nother==1.0 --hash=sha256:{S256}",
    "hash-in-a-comment": f"pkg @ {URL} # --hash=sha256:{S256}",
    "hash-in-the-marker": f"pkg @ {URL} ; extra == '--hash=sha256:{S256}'",
}

ACCEPTED = {
    "sha256": f"pkg @ {URL} --hash=sha256:{S256}",
    "sha256-upper-hex": f"pkg @ {URL} --hash=sha256:{S256.upper()}",
    "sha384": f"pkg @ {URL} --hash=sha384:{S384}",
    "sha512": f"pkg @ {URL} --hash=sha512:{S512}",
    "space-form": f"pkg @ {URL} --hash sha256:{S256}",
    "two-strong": f"pkg @ {URL} --hash=sha256:{S256} --hash=sha512:{S512}",
    "continued": f"pkg @ {URL} \\\n  --hash=sha256:{S256}",
    "bare-url": f"{URL} --hash=sha256:{S256}",
    "marker": f"pkg @ {URL} ; python_version >= '3.9' --hash=sha256:{S256}",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_a_wheel_url_without_a_strong_pin_is_refused_naming_the_line(name: str) -> None:
    text = f"good==1.0\n# a comment\n{REFUSED[name]}\n".encode()
    with pytest.raises(EnvError) as exc:
        normalize_requirements(text, wheels_only=True)
    assert exc.value.reason == "requirements_rejected"
    assert exc.value.failure_type is FailureType.ENV_ERROR
    message = str(exc.value)
    assert "requirements line 3" in message
    assert "--hash=sha256:" in message and "only_binary" in message


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_a_pinned_wheel_url_is_accepted_and_hashes_as_before(name: str) -> None:
    text = f"{ACCEPTED[name]}\n".encode()
    kept = normalize_requirements(text, wheels_only=True)
    assert kept == normalize_requirements(text)  # same bytes: no env hash changes
    assert b"--hash=sha" in kept


def test_requirements_by_name_need_no_hash() -> None:
    text = b"pkg==1.0\nother>=2 ; python_version >= '3.9'\n"
    assert normalize_requirements(text, wheels_only=True) == normalize_requirements(
        text
    )


def test_the_owner_who_switched_wheels_only_off_is_not_asked_for_a_hash() -> None:
    """``only_binary = false`` is the owner's escape hatch for everything the
    wheels-only gate refuses; the pin rule is part of that gate."""
    assert normalize_requirements(f"pkg @ {URL}\n".encode(), wheels_only=False)
