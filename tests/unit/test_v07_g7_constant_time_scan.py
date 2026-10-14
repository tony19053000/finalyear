"""V0.7 G7 (brief §4 item 22, THREAT_MODEL §6): every comparison of a token,
secret, password, signature or a hash of one goes through
``hmac.compare_digest`` - checked over the whole of ``src/`` by reading its
syntax tree, so a new ``presented == expected`` fails here.

The scan flags ``==`` / ``!=`` whose operands name such a value: a variable or
attribute called ``token``, ``*_token``, ``secret``, ``password``, ``pw``,
``signature``, ``mac``, ``token_hash``, ``pw_hash``, ``key_hash``,
``api_key`` (whole ``_``-separated words, any case). Not flagged, by rule:

* ``len(...)`` operands (a length is not the value) and comparisons with a
  literal constant (``is None``-style checks, versions);
* the base object of an attribute (``Secret.name`` is the model's name
  column, not a secret);
* ``fencing_token`` - the attempt epoch counter (ADR-0015), public by design;
* ALL-CAPS constants (``TOKEN_VERSION``, ``TOKEN_BYTES``) and row ids
  (``token_id``, ``*_id``).

Everything else that matches is either fixed or listed in :data:`ALLOWED`
with the reason it is not a credential check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

SRC = Path(__file__).resolve().parents[2] / "src" / "meshforge"

SENSITIVE = re.compile(
    r"(?i)(?:^|_)(?:token|secret|password|passwd|pw|signature|mac|token_hash|"
    r"pw_hash|key_hash|api_key)(?:_|$)"
)
NOT_SECRET_NAMES = frozenset({"fencing_token"})

_EPOCH = "the attempt's fencing epoch (a public counter) held in a variable named token"

#: (path under src/meshforge, the comparison as ``ast.unparse`` prints it) ->
#: why it is not a credential check that needs a constant-time compare.
ALLOWED: dict[tuple[str, str], str] = {
    ("controller/auth/users.py", "ApiToken.token_hash == wanted"): (
        "a SQL WHERE on the SHA-256 of a 256-bit random token (an index lookup "
        "in PostgreSQL, not a Python compare); timing could reveal at most a "
        "prefix of the hash of an unknown random value"
    ),
    ("controller/enrollment/invites.py", "JoinToken.token_hash == token_hash"): (
        "a SQL WHERE on the SHA-256 of a 128-bit random join token (index "
        "lookup, as for API tokens)"
    ),
    ("controller/store/settings.py", "unquote(password) == DEV_DB_PASSWORD"): (
        "the operator's own configured DB password checked against the "
        "published dev default (D145d) - no attacker-supplied value"
    ),
    ("agent/execution/executor.py", "token == spec.fetch_token"): (
        "the agent's own bookkeeping: is the refreshed fetch token the one it "
        "already holds - both values came from the controller over mTLS"
    ),
    ("agent/execution/executor.py", "attempt.upload_token == used"): (
        "agent bookkeeping: which of its own upload grants a finished upload "
        "used (both values are the agent's)"
    ),
    ("agent/execution/executor.py", "attempt.upload_token != used"): (
        "agent bookkeeping (as above)"
    ),
    ("agent/execution/executor.py", "attempt.fetch_token == used"): (
        "agent bookkeeping: which of its own fetch tokens a request used"
    ),
    ("agent/execution/executor.py", "attempt.fetch_token != used"): (
        "agent bookkeeping (as above)"
    ),
    ("agent/execution/executor.py", "attempt.token != msg.fencing_token"): _EPOCH,
    ("agent/execution/executor.py", "attempt.token == msg.fencing_token"): _EPOCH,
    ("agent/execution/executor.py", "existing.token == token"): _EPOCH,
    ("controller/jobs/recovery.py", "attempt.fencing_token != token"): _EPOCH,
    ("controller/jobs/store.py", "token != fencing_token"): _EPOCH,
    ("controller/fabric_token.py", "signature == self._loaded.signature"): (
        "a file stat tuple (size, mtime, inode, mode) deciding a re-read - "
        "named 'signature', holds nothing secret"
    ),
}


def _names(operand: ast.expr) -> list[str]:
    """Identifiers an operand names, without attribute bases and ``len()``."""
    if (
        isinstance(operand, ast.Call)
        and isinstance(operand.func, ast.Name)
        and operand.func.id == "len"
    ):
        return []
    bases = {
        id(node.value) for node in ast.walk(operand) if isinstance(node, ast.Attribute)
    }
    out: list[str] = []
    for node in ast.walk(operand):
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.Name) and id(node) not in bases:
            out.append(node.id)
    return out


def _sensitive(name: str) -> bool:
    if name in NOT_SECRET_NAMES or name.isupper() or name.endswith("_id"):
        return False
    return bool(SENSITIVE.search(name))


def flagged(source: str) -> list[tuple[int, str]]:
    """``(line, comparison)`` for every ``==``/``!=`` on a sensitive name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        if any(isinstance(o, ast.Constant) for o in operands):
            continue
        names = [n for o in operands for n in _names(o)]
        if any(_sensitive(n) for n in names):
            found.append((node.lineno, ast.unparse(node)))
    return found


def test_the_scanner_flags_what_it_should_and_nothing_else() -> None:
    source = """
presented == expected_token
a.api_token != b
token_hash == stored
user.password == given
if len(code.token) != 16: pass
attempt.fencing_token == msg.fencing_token
Secret.name == name
body["v"] != TOKEN_VERSION_X
x == None
hmac.compare_digest(a_token, b_token)
"""
    hits = [text for _, text in flagged(source)]
    assert hits == [
        "presented == expected_token",
        "a.api_token != b",
        "token_hash == stored",
        "user.password == given",
    ]


def test_no_secret_is_compared_with_eq_anywhere_in_src() -> None:
    unexpected: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("common/proto/"):
            continue  # generated code
        for line, text in flagged(path.read_text(encoding="utf-8")):
            key = (rel, text)
            if key in ALLOWED:
                used.add(key)
            else:
                unexpected.append(f"src/meshforge/{rel}:{line}: {text}")
    assert unexpected == [], (
        "compare tokens/secrets/hashes of them with hmac.compare_digest "
        "(or add an ALLOWED entry with the reason it is not a credential "
        f"check): {unexpected}"
    )
    # A stale allow-list entry is a silent hole for the next edit there.
    assert set(ALLOWED) == used, sorted(set(ALLOWED) - used)


def test_the_credential_checks_do_use_compare_digest() -> None:
    """Positive control: the verification sites the threat model names."""
    for rel in (
        "controller/fabric_token.py",  # the legacy fabric token
        "controller/auth/passwords.py",  # scrypt password hashes
        "controller/auth/users.py",  # API token hashes
        "common/join_code.py",  # the code's CA pin
        "common/pki.py",  # certificate fingerprint pins
        "agent/peer/auth.py",  # peer token claims
        "controller/secret_box.py",  # secrets master key id
    ):
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "compare_digest"
                )
                or (
                    isinstance(node.func, ast.Name) and node.func.id == "compare_digest"
                )
            )
        ]
        assert calls, f"{rel}: no compare_digest call (a docstring mention is not one)"
