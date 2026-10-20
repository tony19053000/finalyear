"""V0.7 G7 (brief §4 item 22, THREAT_MODEL §6): every comparison of a token,
secret, password, signature or a hash of one goes through
``hmac.compare_digest`` - checked over the whole of ``src/`` by reading its
syntax tree, so a new ``presented == expected`` fails here.

The scan (widened in fix round 1, D167 TQ-6) flags ``==`` / ``!=`` / ``in`` /
``not in`` where either side names a credential-like value: a variable,
attribute or call result whose name contains (as a whole ``_``-separated
word, any case, plural too) ``token``, ``secret``, ``password``, ``passwd``,
``pw``, ``pin``, ``fingerprint``, ``hash``, ``hexdigest``, ``mac``, ``sig``,
``signature``, ``expected``, ``presented``, ``bearer``, ``credential``,
``cookie``, ``code`` or ``api_key``, or a ``.digest()`` / ``.hexdigest()``
call. Not flagged, by rule:

* ``len(...)`` operands and names ending ``_len`` (a length is not the value);
* comparisons with ``None``, a bool, a number or an empty string (``is
  None``-style checks, versions) - a **non-empty** string/bytes literal is
  flagged (``password == "hunter2"`` is a hard-coded credential);
* the base object of an attribute (``Secret.name`` is the model's name
  column, not a secret);
* ``fencing_token`` - the attempt epoch counter (ADR-0015), public by design;
  ``status_code`` - an HTTP status;
* ALL-CAPS constants (``TOKEN_VERSION``, ``TOKEN_BYTES``) and row ids
  (``token_id``, ``*_id``).

Everything else that matches is either fixed or listed in :data:`ALLOWED`
with the reason it is not a credential check. The wide word list makes most
entries public data (certificate/key fingerprints, content digests, error
codes); each was read, none is a secret compared in Python.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

SRC = Path(__file__).resolve().parents[2] / "src" / "meshforge"

SENSITIVE = re.compile(
    r"(?i)(?:^|_)(?:tokens?|secrets?|passwords?|passwd|pw|pins?|fingerprints?|"
    r"hexdigest|hash(?:es)?|mac|sig|signatures?|expected|presented|bearer|"
    r"credentials?|cookies?|code|api_key)(?:_|$)"
)
NOT_SECRET_NAMES = frozenset({"fencing_token", "status_code"})
#: Calls whose result is a hash value (``h.digest()``, ``sha256(x).hexdigest()``).
DIGEST_CALLS = frozenset({"digest", "hexdigest"})
_DIGEST_CALL = "digest()"
#: Comparison operators the scan looks at (``in`` is ``==`` on each element).
_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)

_EPOCH = "the attempt's fencing epoch (a public counter) held in a variable named token"
_HTTP = "an HTTP/gRPC status or error code, public protocol data"
_CODE = "a public refusal/failure code from a fixed vocabulary, not a join code"
_CA_FP = (
    "the SHA-256 fingerprint of a CA certificate - public (it is printed by "
    "`fabric show` and embedded in every join code)"
)
_KEY_FP = "a public-key fingerprint (a hash of public data), not a secret"
_CONTENT = "a content digest of a blob/tree (content addressing, public data)"

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
    # V0.10 G4: a lane's own token bookkeeping and the lane's epoch check
    ("agent/execution/lane_staging.py", "attempt.fetch_token == used"): (
        "agent bookkeeping: which of its own fetch tokens a chunk staging used"
    ),
    ("agent/cas/materialise.py", "Path(os.path.realpath(path.parent)) != expected"): (
        "a filesystem path compare (a folder became a link), no credential"
    ),
    ("agent/execution/lane.py", "attempt.token != inner.fencing_token"): _EPOCH,
    ("controller/split/store.py", "int(attempt.fencing_token) != token"): _EPOCH,
    ("agent/execution/executor.py", "attempt.token != msg.fencing_token"): _EPOCH,
    ("agent/execution/executor.py", "attempt.token == msg.fencing_token"): _EPOCH,
    ("agent/execution/executor.py", "existing.token == token"): _EPOCH,
    ("controller/jobs/recovery.py", "attempt.fencing_token != token"): _EPOCH,
    ("controller/jobs/store.py", "token != fencing_token"): _EPOCH,
    ("controller/fabric_token.py", "signature == self._loaded.signature"): (
        "a file stat tuple (size, mtime, inode, mode) deciding a re-read - "
        "named 'signature', holds nothing secret"
    ),
    # --- added when the scan was widened (fix round 1, D167 TQ-6) ---
    ("agent/execution/executor.py", "(attempt_id, token) in self._finished"): _EPOCH,
    ("agent/execution/executor.py", "key not in attempt.peer_tokens"): (
        "a dict lookup by (tree id, node id) - the key holds no token value"
    ),
    ("agent/benchmarks/cache.py", "scores.fingerprint != current_fingerprint"): (
        "the hardware fingerprint of the laptop's benchmark cache (public)"
    ),
    (
        "agent/enrollment.py",
        "pki.cert_fingerprint(ca) != pki.cert_fingerprint(pinned_ca)",
    ): _CA_FP,
    (
        "agent/enrollment.py",
        "pki.cert_fingerprint(ca) != current.ca_fingerprint",
    ): _CA_FP,
    ("agent/identity.py", "meta.get('key_fingerprint_sha256') != fingerprint"): _KEY_FP,
    (
        "agent/identity.py",
        "current.get('key_fingerprint_sha256') != identity.fingerprint",
    ): (_KEY_FP),
    ("agent/identity.py", "_fingerprint(staged) != new_fp"): _KEY_FP,
    (
        "controller/enrollment/renewal.py",
        "key_fp != presented.public_key_fingerprint",
    ): (_KEY_FP),
    (
        "controller/enrollment/renewal.py",
        "node.pending_public_key_fingerprint != node.public_key_fingerprint",
    ): _KEY_FP,
    (
        "controller/blobs.py",
        "hashlib.sha256(data).hexdigest() != tree_id.hex",
    ): _CONTENT,
    ("common/tree.py", "tree_id != expected"): _CONTENT,
    ("controller/planner/estimator.py", "env_hash in node.cached_envs"): (
        "an environment's content hash (public, advertised by every agent)"
    ),
    ("agent/envs/hash.py", "'=' not in token"): (
        "parsing a pip requirement word named token (`--opt=value`), no credential"
    ),
    ("controller/api/auth.py", "' ' in token"): (
        "rejects a malformed Authorization header (a space inside the bearer "
        "value) before any lookup; reveals only that the presented value had "
        "a space, which the caller already knows"
    ),
    ("agent/main.py", "code == '-'"): (
        "the `-` argument sentinel meaning 'read the join code from stdin'; "
        "the real code is checked by the controller with compare_digest on its hash"
    ),
    ("agent/gc/service.py", "generation != self._pins_gen"): (
        "a counter of GC pin requests (cache pins, not a PIN)"
    ),
    ("agent/execution/subprocess_runtime.py", "sig == _SIGKILL"): (
        "a POSIX signal number"
    ),
    ("agent/benchmarks/runner.py", "exc.code in _UNAVAILABLE"): _HTTP,
    (
        "agent/enrollment.py",
        "code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)",
    ): _HTTP,
    ("cli/job_commands.py", "code != CliExit.OK"): "a process exit code",
    ("agent/session/policy.py", "code in _FATAL_CODES"): _CODE,
    ("agent/session/policy.py", "reject_code == codes.REJECT_CODE_STALE_EPOCH"): _CODE,
    ("agent/session/policy.py", "reject_code == codes.REJECT_CODE_PROTOCOL_VERSION"): (
        _CODE
    ),
    ("controller/jobs/planning.py", "refused.code in _GPU_GONE"): _CODE,
    ("controller/planner/types.py", "self.code in STATIC_CODES"): _CODE,
}


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _names(operand: ast.expr) -> list[str]:
    """Identifiers an operand names, without attribute bases and ``len()``;
    a ``.digest()``/``.hexdigest()`` call is reported as ``digest()``."""
    if isinstance(operand, ast.Call) and _call_name(operand) == "len":
        return []
    bases = {
        id(node.value) for node in ast.walk(operand) if isinstance(node, ast.Attribute)
    }
    out: list[str] = []
    for node in ast.walk(operand):
        if isinstance(node, ast.Call) and _call_name(node) in DIGEST_CALLS:
            out.append(_DIGEST_CALL)
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.Name) and id(node) not in bases:
            out.append(node.id)
    return out


def _sensitive(name: str) -> bool:
    if name == _DIGEST_CALL:
        return True
    if name in NOT_SECRET_NAMES or name.isupper() or name.endswith(("_id", "_len")):
        return False
    return bool(SENSITIVE.search(name))


def _harmless_literal(operand: ast.expr) -> bool:
    """``None``, a bool, a number or an empty string/bytes: never a credential
    value. A non-empty string literal is not harmless (hard-coded secret)."""
    if not isinstance(operand, ast.Constant):
        return False
    value = operand.value
    return not (isinstance(value, str | bytes) and value)


def flagged(source: str) -> list[tuple[int, str]]:
    """``(line, comparison)`` for every ``==``/``!=``/``in``/``not in`` on a
    sensitive name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _OPS) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        if any(_harmless_literal(o) for o in operands):
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
token in allowed
h.hexdigest() != wanted
user.password == "hunter2"
user.password == ""
attempt.status_code == 200
if len(body) != expected_len: pass
x.pin is not None
"""
    hits = [text for _, text in flagged(source)]
    assert hits == [
        "presented == expected_token",
        "a.api_token != b",
        "token_hash == stored",
        "user.password == given",
        "token in allowed",
        "h.hexdigest() != wanted",
        "user.password == 'hunter2'",
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
