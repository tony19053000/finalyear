"""Exact-value redaction of job secrets in logs (V0.7 G6, roadmap 0.7.10, D144).

The agent (log capture) and the controller (log store, live streams, final log
copy) replace every occurrence of a secret value **and of its common
encodings** before a byte is written or streamed:

* the raw bytes;
* base64 - standard and URL-safe alphabets, padded and unpadded;
* hex - lower and upper case;
* URL-encoding - ``%XX`` upper and lower case, with ``+`` or ``%20`` for a space.

:class:`StreamRedactor` works on a byte stream delivered in chunks: it keeps
back the last ``max form length - 1`` bytes of what it was given, so a value
split across two chunks is still found; :meth:`StreamRedactor.flush` releases
the tail at the end. Two replacement styles:

* ``[secret:NAME]`` (the agent: it owns its stream's offsets);
* **length-preserving** (the controller: its log files are addressed by the
  agent's byte offsets, so a replacement must not change lengths) - the marker
  cut or padded with ``*`` to the match's length.

Values shorter than :data:`~meshforge.common.secret_names.MIN_SECRET_BYTES` are
refused at ``compute secret set``: a 1-3 byte value would match all over normal
output. **Honest limit:** this stops *accidents*; a workload that wants to leak
its own secret can (reversed, split, another encoding) - THREAT_MODEL.

:data:`ACTIVE` is the process-wide set of values currently in use (an attempt
running on this agent, a controller holding a job's values); the structlog
processor (:func:`scrub_active`) replaces them in every string of our own logs.
"""

from __future__ import annotations

import base64
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import quote, quote_plus

__all__ = [
    "ACTIVE",
    "ActiveSecrets",
    "StreamRedactor",
    "encoded_forms",
    "marker",
    "redact_bytes",
    "restore_markers",
    "scrub_active",
]

_MIN_FORM_BYTES: Final = 4


def marker(name: str) -> bytes:
    return f"[secret:{name}]".encode("ascii")


def _lower_percent(text: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), text)


def encoded_forms(value: bytes) -> set[bytes]:
    """The raw value and every encoding listed in the module docstring."""
    forms = {value}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        padded = encode(value)
        forms.add(padded)
        forms.add(padded.rstrip(b"="))
    forms.add(value.hex().encode("ascii"))
    forms.add(value.hex().upper().encode("ascii"))
    for quoted in (quote(value, safe=""), quote_plus(value, safe="")):
        forms.add(quoted.encode("ascii"))
        forms.add(_lower_percent(quoted).encode("ascii"))
    return {form for form in forms if len(form) >= _MIN_FORM_BYTES}


def _length_preserving(name: str, length: int) -> bytes:
    mark = marker(name)
    if length <= len(mark):
        return b"*" * length
    return mark + b"*" * (length - len(mark))


class _Patterns:
    def __init__(self, secrets: Mapping[str, bytes]) -> None:
        owner: dict[bytes, str] = {}
        for name in sorted(secrets):
            for form in encoded_forms(secrets[name]):
                owner.setdefault(form, name)
        # Longest first: at one position the longest encoding wins.
        ordered = sorted(owner, key=lambda f: (-len(f), f))
        self.owner = owner
        self.max_len = max((len(f) for f in ordered), default=0)
        self.regex = (
            re.compile(b"|".join(re.escape(f) for f in ordered)) if ordered else None
        )


class StreamRedactor:
    """Redact one byte stream fed in chunks (module docstring)."""

    def __init__(
        self, secrets: Mapping[str, bytes], *, length_preserving: bool = False
    ) -> None:
        self._p = _Patterns(secrets)
        self._preserve = length_preserving
        self._held = b""
        self.replaced = 0
        """Matches replaced so far (tests and metrics; never the values)."""

    @property
    def held(self) -> int:
        """Bytes kept back for the next chunk."""
        return len(self._held)

    def _replacement(self, form: bytes) -> bytes:
        name = self._p.owner[form]
        return _length_preserving(name, len(form)) if self._preserve else marker(name)

    def feed(self, data: bytes) -> bytes:
        """The redacted bytes that are safe to emit now."""
        regex = self._p.regex
        if regex is None:
            return data
        buf = self._held + data
        safe = max(0, len(buf) - (self._p.max_len - 1))
        out: list[bytes] = []
        pos = 0
        for match in regex.finditer(buf):
            if match.start() >= safe:
                break
            out.append(buf[pos : match.start()])
            out.append(self._replacement(match.group(0)))
            self.replaced += 1
            pos = match.end()
        emit_to = max(pos, safe)
        out.append(buf[pos:emit_to])
        self._held = buf[emit_to:]
        return b"".join(out)

    def flush(self) -> bytes:
        """Release what is held (the end of the stream)."""
        regex = self._p.regex
        rest, self._held = self._held, b""
        if regex is None or not rest:
            return rest
        out = regex.sub(lambda m: self._count(self._replacement(m.group(0))), rest)
        return out

    def _count(self, replacement: bytes) -> bytes:
        self.replaced += 1
        return replacement


def redact_bytes(
    data: bytes, secrets: Mapping[str, bytes], *, length_preserving: bool = False
) -> bytes:
    """One-shot :class:`StreamRedactor` over a whole buffer."""
    redactor = StreamRedactor(secrets, length_preserving=length_preserving)
    return redactor.feed(data) + redactor.flush()


class ActiveSecrets:
    """Values in use in this process, by holder (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_holder: dict[str, dict[str, bytes]] = {}
        self._text: list[tuple[str, str]] = []

    def add(self, holder: str, secrets: Mapping[str, bytes]) -> None:
        if not secrets:
            return
        with self._lock:
            self._by_holder[holder] = dict(secrets)
            self._rebuild()

    def remove(self, holder: str) -> None:
        with self._lock:
            if self._by_holder.pop(holder, None) is not None:
                self._rebuild()

    def __bool__(self) -> bool:
        return bool(self._text)

    def _rebuild(self) -> None:
        pairs: dict[str, str] = {}
        for secrets in self._by_holder.values():
            for name, value in secrets.items():
                for form in encoded_forms(value):
                    try:
                        pairs.setdefault(form.decode("utf-8"), name)
                    except UnicodeDecodeError:
                        continue
        self._text = sorted(pairs.items(), key=lambda kv: -len(kv[0]))

    def scrub(self, text: str, *, placeholder: bool = False) -> str:
        """Every active form -> ``[secret:NAME]`` (or, with ``placeholder``,
        a private-use token :func:`restore_markers` turns into the marker -
        so a later ``key: value`` rule cannot mangle ``secret:NAME``)."""
        pairs = self._text
        for form, name in pairs:
            if form in text:
                text = text.replace(
                    form,
                    f"{_PH_OPEN}{name}{_PH_CLOSE}"
                    if placeholder
                    else f"[secret:{name}]",
                )
        return text

    def names(self) -> Iterable[str]:
        with self._lock:
            return sorted({n for s in self._by_holder.values() for n in s})


ACTIVE: Final = ActiveSecrets()


def scrub_active(text: str, *, placeholder: bool = False) -> str:
    """:data:`ACTIVE`'s values (and encodings) out of ``text``."""
    return ACTIVE.scrub(text, placeholder=placeholder) if ACTIVE else text


_PH_OPEN: Final = "\ue000"
_PH_CLOSE: Final = "\ue001"
_PH_RE: Final = re.compile(f"{_PH_OPEN}([A-Z0-9_]{{1,64}}){_PH_CLOSE}")


def restore_markers(text: str) -> str:
    """Placeholders left by ``scrub_active(..., placeholder=True)`` ->
    ``[secret:NAME]``."""
    if _PH_OPEN not in text:
        return text
    return _PH_RE.sub(lambda m: f"[secret:{m.group(1)}]", text)
