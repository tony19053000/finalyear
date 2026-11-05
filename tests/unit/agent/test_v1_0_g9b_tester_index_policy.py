"""V1.0 G9-B tester: D436 extra_index_urls stay credential-free everywhere,
and D433 fingerprint edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.agent.envs import EnvError, normalize_requirements
from meshforge.agent.envs.policy import env_policy_capability, env_policy_fingerprint
from meshforge.agent.settings import EnvsSettings
from meshforge.common.capabilities import env_policy_of

pytestmark = pytest.mark.unit

TORCH = "https://download.pytorch.org/whl/cu126"
SECRET = "hunter2-SECRET"


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:{SECRET}@host/simple",
        f"https://{SECRET}@host/simple",
        f"https://:{SECRET}@host/simple",
        f"HTTPS://user:{SECRET}@host/simple",
    ],
)
def test_credentials_rejected_and_the_secret_is_not_echoed(url: str) -> None:
    with pytest.raises(ValidationError) as info:
        EnvsSettings(extra_index_urls=[url])
    # the error ends up in the agent log / console: it must not carry the secret
    assert SECRET not in str(info.value)


def test_the_same_for_index_url_baseline() -> None:
    with pytest.raises(ValidationError) as info:
        EnvsSettings(index_url=f"https://u:{SECRET}@h/s")
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        f"https://h/simple?token={SECRET}",
        f"https://h/simple#{SECRET}",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="G9B-T1 (minor): a token in ?query / #fragment is accepted although "
    "it would reach process listings like userinfo",
)
def test_query_or_fragment_secrets_are_not_accepted(url: str) -> None:
    """A token in the query string reaches process listings like userinfo."""
    with pytest.raises(ValidationError):
        EnvsSettings(extra_index_urls=[url])


def test_fingerprint_and_tag_carry_no_url_text() -> None:
    cfg = EnvsSettings(extra_index_urls=[TORCH])
    tag = env_policy_capability(cfg)
    assert "pytorch" not in tag and "http" not in tag
    assert env_policy_of([tag]) == env_policy_fingerprint(cfg)


def test_fingerprint_order_and_dedupe() -> None:
    a = EnvsSettings(extra_index_urls=[TORCH, "https://x.example/s"])
    b = EnvsSettings(extra_index_urls=["https://x.example/s", TORCH])
    dup = EnvsSettings(extra_index_urls=[TORCH, TORCH])
    one = EnvsSettings(extra_index_urls=[TORCH])
    assert env_policy_fingerprint(a) != env_policy_fingerprint(b)
    assert env_policy_fingerprint(dup) == env_policy_fingerprint(one)
    assert env_policy_fingerprint(one) != env_policy_fingerprint(EnvsSettings())


def test_fingerprint_stable_across_unrelated_settings(tmp_path: Path) -> None:
    base = EnvsSettings()
    other = EnvsSettings(dir=tmp_path)
    assert env_policy_fingerprint(base) == env_policy_fingerprint(other)


@pytest.mark.parametrize(
    "line",
    [
        f"--extra-index-url {TORCH}",
        f"--extra-index-url={TORCH}",
        f"  --extra-index-url   {TORCH}",
        f"--index-url {TORCH}",
        f"-i {TORCH}",
        f"--find-links {TORCH}",
        f"-f {TORCH}",
        "--trusted-host download.pytorch.org",
    ],
)
def test_job_level_index_options_refused_under_only_binary(line: str) -> None:
    with pytest.raises(EnvError):
        normalize_requirements(f"{line}\ntorch\n".encode(), wheels_only=True)


def test_job_level_refusal_independent_of_owner_extra_index() -> None:
    # the owner's list is not an excuse: the job file is still screened
    with pytest.raises(EnvError):
        normalize_requirements(
            f"--extra-index-url {TORCH}\n".encode(), wheels_only=True
        )
    # an ordinary torch pin is fine
    normalize_requirements(b"torch==2.5.1\n", wheels_only=True)


@pytest.mark.parametrize(
    "tags",
    [
        ["env-policy:" + "A" * 12],  # uppercase
        ["env-policy:" + "a" * 11],
        ["env-policy:" + "a" * 13],
        ["env-policy:" + "a" * 12, "env-policy:" + "b" * 12],
        ["env-policy:"],
        [],
    ],
)
def test_malformed_or_conflicting_policy_tags_are_unknown(tags: list[str]) -> None:
    assert env_policy_of(tags) is None
