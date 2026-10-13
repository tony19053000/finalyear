"""Exact-value redaction of job secrets in logs (V0.7 G6, roadmap 0.7.10, D144).

The agent (log capture) and the controller (log store, live streams, final log
copy) replace every occurrence of a secret value **and of its common
encodings** before a byte is written or streamed:

* the raw bytes;
* base64 - standard and URL-safe alphabets, padded and unpadded;
* hex - lower and upper case;
* URL-encoding - ``%XX`` upper and lower case, with ``+`` or ``%20`` for a space.

**Not covered** (documented in THREAT_MODEL WS-I3 and OPERATIONS): any other
transformation - JSON/``repr``-escaped values (a multi-line or quoted value
through ``json.dumps``), a value embedded in a larger base64 blob (HTTP Basic
``user:secret``), reversed, split, base32, double encoding. This stops
*accidents*; a workload that wants to leak its own secret can.

Matcher (D162, T-8)
-------------------
:class:`SecretMatcher` is one **immutable, shareable** multi-pattern matcher
per attempt; :class:`StreamRedactor` holds only per-stream state. It is an
Aho-Corasick automaton over every form, in a shape that costs milliseconds to
build for the largest job (256 KiB of values -> ~5.5 MB of forms):

* the forms sit in a **radix trie** (one node per branch, edges are slices of
  the forms - a few hundred nodes, never one node per byte);
* a state is "the longest suffix of the stream that is a prefix of a form" =
  a start position plus a (node, depth) pair; advancing along an edge is one
  ``memcmp``;
* where no form is in progress, a compiled regex of the forms' 4-byte
  prefixes (every form is >= 4 bytes) jumps to the next place one could start;
* the **failure function** is the automaton run over the forms themselves
  (``fail(p[:d])`` is the state after reading ``p[1:d]``), computed lazily and
  remembered per form (:class:`_SelfScan`), together with the forms that occur
  *inside* a form. Every query is a bisect, so a stream never re-reads a byte:
  the work is linear in the output whatever the values look like.

Termination (D163, T-17)
------------------------
Every loop has a measure that only moves one way and is bounded:

* ``_lcp``: the gallop adds ``step >= 16`` to ``lo`` (bounded by ``hi``); the
  binary search halves ``hi - lo``.
* ``SecretMatcher._build`` / ``_anchor_regex``: ``i`` moves to ``j > i``; a
  recursive call has a strictly larger ``depth`` (at most the longest form).
* ``_Walker.run``: the measure is :meth:`_Walker.key`, an integer checked at
  the top of every turn. Its terms: ``j`` = the text index where the current
  candidate starts (the earliest start still alive); ``d`` = how many of its
  bytes are matched, so ``j + d`` is the read position; ``c`` = the index the
  search goes on from when there is no candidate (no match can start before
  it); ``limit`` = the end of the text this call may read.
  ``key = 5 * j + d`` while a candidate is followed (that is ``4 * start +
  position``) and ``key = 5 * c`` while searching. A turn that does not return
  makes the key strictly larger:

  - *extend* by ``n >= 1`` matched bytes: ``d`` grows, key ``+ n``;
  - *fall back* to a shorter candidate that starts ``s`` bytes later
    (``1 <= s <= d - 4``: a proper suffix of at least 4 bytes): ``j + s``,
    ``d - s``, key ``+ 4 * s >= 4``;
  - *give up* the candidate and search on from ``c = j + d - 3``: the key
    goes from ``5 * j + d`` to ``5 * (j + d - 3)``, that is ``+ (4 * d - 15)``.
    A candidate begins at an anchor hit (4 matched bytes) and a fallback keeps
    at least 4, so ``d >= 4`` here and the step is ``>= 1`` - the search
    restarts 3 bytes back, but never at or before where the candidate began;
  - *search* finds a hit at ``h >= c``: the candidate starts there with its
    first byte(s) matched in the same turn, key ``>= 5 * h + 1 > 5 * c``.

  ``j + d <= limit`` and ``c <= limit``, so the key never passes
  ``5 * limit``: a call makes at most ``5 * limit - (key on entry) + 1`` turns.
* ``_SelfScan.advance(depth)``: at most one ``run``, up to ``limit =
  min(depth, len(form))``. It returns None in two ways: **at once, without
  any step**, when ``front >= limit`` already; or after a ``run`` that
  brought ``front`` up to ``limit`` (that run stepped: ``front`` was below
  ``limit`` before). Or it returns a need ``(form, d)`` with ``d < depth`` -
  a self-scan starts at byte 1, so the prefix it follows is shorter than the
  text it has read. A ``run`` that stops with ``front < limit``, and a need
  with ``d >= depth``, raise.
* ``SecretMatcher.ensure(need)``, with ``D`` = the depth of ``need``: a stack
  of needs, one ``advance`` of the top per turn; None pops, a need pushes.
  ``ticks`` counts the steps of all walkers; ``idle`` counts the turns in a
  row without a new tick.

  - *It always ends, whatever the records hold.* More than ``D + 2`` idle
    turns raise, so between two ticks there are at most ``D + 3`` turns. And
    ticks are finite. A tick of a self-scan's walker raises its key (``<= 5 *
    len(form)``), or turns it from searching to following at the place it
    searched from (once per place), or is the last, empty search of a
    ``run`` - and ``advance`` starts a ``run`` only with ``front`` below the
    limit and accepts its end only with ``front`` at the limit, so every
    ``run`` that ends raised ``front`` (``<= len(form)``). That is at most
    ``12 * len(form)`` ticks per form, and ``ensure`` returns or raises
    within ``(D + 3) * (12 * total form bytes + 1)`` turns.
  - *The idle budget is enough: a right matcher never uses it up.* A need is
    raised only past the anchor (depth ``> 4``) and depths strictly shrink up
    the stack, so the stack holds at most ``D - 4`` needs: at most ``D - 5``
    pushes can follow each other. An idle turn is a push (the scan needed
    another scan before it could step) or the step-less None above. Inside
    ``ensure`` that None does not happen: a need ``(k, d)`` is raised only
    while scan ``k`` is short of ``d``, and the entries pushed above it ask
    the same scan only for smaller depths (a ``run`` never moves ``front``
    past its ``limit``), so a need is still open whenever its turn comes.
    Every pop therefore follows a ``run`` that stepped - a tick. An idle run
    is pushes only: at most ``D - 5 < D + 2`` turns.
  - What the count catches: a scan that answers "done" although it did not
    move, so that the scan below asks again - the T-17 defect. It is reported
    (``RedactionStallError``) instead of repeated for ever.
* ``StreamRedactor.feed``: each wait is followed by the step waited for, so
  the stream walker's measure rises between two waits.

T-17 was a break of the second-to-last line: after an anchor hit, ``front``
(and the new episode's reach) kept their old value, so a scan asked for a
depth it had already jumped past answered "done" with ``front < depth``, and
the scan that asked, asked again - for ever. The same half-updated state made
a waiting scan's episode list unsorted, which could hide a match (a value
printed in clear). The hit is now one step: candidate, ``front`` and episode
together.

**The measures are also checked while running**: a loop that would go round
without moving raises :class:`RedactionStallError`; the stream (and every
other stream of that matcher) then emits ``*`` for every byte
(:attr:`StreamRedactor.failed`). A bug here can cost a log, never freeze an
event loop and never print a value.

Overlaps (T-2): **the union of all matched intervals is redacted**. A merged
interval gets ONE replacement, named after the match that starts first (the
longest one if several start there): ``[secret:NAME]`` on the agent; on the
controller the length-preserving form (below). Two matches that only touch
(one ends where the next starts) stay two replacements.

Hold-back (T-3): a feed keeps back only the longest suffix of the stream that
is a proper prefix of some form - a line that cannot begin a secret is
released at once. :meth:`StreamRedactor.flush` releases the tail at the end.

Two replacement styles:

* ``[secret:NAME]`` (the agent: it owns its stream's offsets);
* **length-preserving** (the controller: its log files are addressed by the
  agent's byte offsets, so a replacement must not change lengths) - the marker
  padded with ``*`` to the interval's length, or only ``*`` when the interval
  is not longer than the marker.

Values shorter than :data:`~meshforge.common.secret_names.MIN_SECRET_BYTES` are
refused at ``compute secret set``: a 1-3 byte value would match all over normal
output.

:data:`ACTIVE` is the process-wide set of values currently in use (an attempt
running on this agent, a controller holding a job's values); the structlog
processor (:func:`scrub_active`) replaces them in every string of our own logs.
"""

from __future__ import annotations

import base64
import re
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping
from typing import Final

__all__ = [
    "ACTIVE",
    "ActiveSecrets",
    "RedactionStallError",
    "SecretMatcher",
    "StreamRedactor",
    "encoded_forms",
    "marker",
    "redact_bytes",
    "restore_markers",
    "scrub_active",
]

_MIN_FORM_BYTES: Final = 4
_ANCHOR: Final = _MIN_FORM_BYTES
"""Every form is at least this long, so its first ``_ANCHOR`` bytes exist."""


class RedactionStallError(RuntimeError):
    """A matcher loop went round without moving forward (module docstring,
    "Termination"). Never expected; raised instead of spinning, and the stream
    then withholds its output (:attr:`StreamRedactor.failed`)."""


def marker(name: str) -> bytes:
    return f"[secret:{name}]".encode("ascii")


def _percent_table(*, upper: bool, plus: bool) -> tuple[bytes, ...]:
    # Same output as urllib.parse.quote(value, safe="") / quote_plus: only
    # ASCII letters, digits and "_.-~" stay; a table keeps it at C speed.
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~"
    fmt = "%{:02X}" if upper else "%{:02x}"
    table = [
        bytes([b]) if b in safe else fmt.format(b).encode("ascii") for b in range(256)
    ]
    if plus:
        table[0x20] = b"+"
    return tuple(table)


_PERCENT_TABLES: Final = tuple(
    _percent_table(upper=upper, plus=plus)
    for upper in (True, False)
    for plus in (False, True)
)


def encoded_forms(value: bytes) -> set[bytes]:
    """The raw value and every encoding listed in the module docstring."""
    forms = {value}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        padded = encode(value)
        forms.add(padded)
        forms.add(padded.rstrip(b"="))
    forms.add(value.hex().encode("ascii"))
    forms.add(value.hex().upper().encode("ascii"))
    for table in _PERCENT_TABLES:
        forms.add(b"".join(map(table.__getitem__, value)))
    return {form for form in forms if len(form) >= _MIN_FORM_BYTES}


def _length_preserving(name: str, length: int) -> bytes:
    mark = marker(name)
    if length <= len(mark):
        return b"*" * length
    return mark + b"*" * (length - len(mark))


def _lcp(text: bytes | bytearray, pos: int, form: memoryview, d: int, k: int) -> int:
    """How many of ``form[d:d+k]`` equal ``text[pos:pos+k]`` from the left.

    ``memcmp`` only (it stops at the first difference): a gallop, then a
    binary search inside the last step.
    """
    if text.startswith(form[d : d + k], pos):
        return k
    lo, hi, step = 0, k, 16
    while lo + step < hi and text.startswith(form[d + lo : d + lo + step], pos + lo):
        lo += step
        step *= 2
    hi = min(hi, lo + step)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if text.startswith(form[d + lo : d + mid], pos + lo):
            lo = mid
        else:
            hi = mid
    return lo


class _Node:
    """Radix-trie node: the prefix ``forms[k][:end]``; its edge is
    ``forms[k][start:end]``; ``term`` is the form equal to that prefix (-1 if
    none)."""

    __slots__ = ("children", "end", "k", "start", "term")

    def __init__(self, k: int, start: int, end: int, term: int) -> None:
        self.k = k
        self.start = start
        self.end = end
        self.term = term
        self.children: dict[int, _Node] = {}


_Need = tuple[int, int]
"""``(form index, depth)``: a self-scan that must reach that depth first."""


class _Walker:
    """The automaton's position in one text.

    Either *searching* (``node is None``: no start before ``c`` can still
    become a match) or *following* a candidate that starts at ``j`` and has
    matched ``d`` bytes, ending in ``node``. ``j`` is always the earliest
    start still alive, so the state is the Aho-Corasick state.
    """

    __slots__ = ("c", "d", "front", "j", "m", "node", "on_match", "scan", "text")

    def __init__(
        self,
        matcher: SecretMatcher,
        text: bytes | bytearray,
        start: int,
        on_match: Callable[[int, int, int], None],
        scan: _SelfScan | None = None,
    ) -> None:
        self.m = matcher
        self.text = text
        self.c = start
        self.j = 0
        self.d = 0
        self.node: _Node | None = None
        self.front = start
        """Everything about positions up to here is recorded (self-scans)."""
        self.on_match = on_match
        self.scan = scan

    def key(self) -> int:
        """The measure of :meth:`run` (module docstring, "Termination"):
        ``5 * j + d`` while following a candidate (``j`` its start, ``d`` its
        matched bytes - that is ``4 * j`` plus the read position ``j + d``),
        ``5 * c`` while searching from ``c``. It never goes down, and it
        rises with every turn of :meth:`run` that does not return."""
        return 5 * self.j + self.d if self.node is not None else 5 * self.c

    def run(self, limit: int) -> _Need | None:
        """Read ``text[:limit]``. Returns None when done (then ``front >=
        limit``), or the self-scan that has to advance first (the caller
        advances it and calls again; nothing was changed)."""
        m = self.m
        text = self.text
        scan = self.scan
        last = self.key() - 1
        while True:
            node = self.node
            key = 5 * self.j + self.d if node is not None else 5 * self.c
            if key <= last:
                raise RedactionStallError("matcher position did not advance")
            last = key
            if node is None:
                hit = m.anchor.search(text, self.c, limit)
                m.ticks += 1
                if hit is None:
                    if limit - (_ANCHOR - 1) > self.c:
                        self.c = limit - (_ANCHOR - 1)
                    if limit > self.front:
                        self.front = limit
                    return None
                # One step, with nothing observable in between: the candidate,
                # ``front`` (no candidate lives before the hit - T-17) and the
                # new episode, whose reach starts at ``front`` so that the
                # episode list stays sorted while this scan waits for another.
                self.j = self.c = hit.start()
                self.d = 0
                if self.j > self.front:
                    self.front = self.j
                self.node = node = m.root
                if scan is not None:
                    scan.open_episode(self.j, self.front)
            j, d = self.j, self.d
            pos = j + d
            if d == node.end:
                if not node.children:
                    need = self._die(node, j, d)
                    if need is not None:
                        return need
                    continue
                if pos >= limit:
                    return None
                child = node.children.get(text[pos])
                if child is None:
                    need = self._die(node, j, d)
                    if need is not None:
                        return need
                    continue
                self.node = node = child
                if scan is not None:
                    scan.enter(node)
            k = min(node.end - d, limit - pos)
            own = scan is not None and node.k == scan.k
            if own:
                # A form read along itself (a periodic value): its own records
                # exist only up to ``front``.
                k = min(k, self.front - d)
            if k <= 0:
                return None
            n = _lcp(text, pos, m.views[node.k], d, k)
            if n:
                nd = d + n
                if nd > _ANCHOR:
                    inner = m.scan(node.k)
                    if not own and inner.front < nd:
                        return (node.k, nd)
                    lo = bisect_right(inner.m_ends, d)
                    hi = bisect_right(inner.m_ends, nd)
                    for s, e, f in inner.m_recs[lo:hi]:
                        self.on_match(j + s, j + e, f)
                self.d = nd
                m.ticks += 1
                if j + nd > self.front:
                    self.front = j + nd
                if scan is not None:
                    scan.e_reach[-1] = j + nd
                if nd == node.end and node.term >= 0:
                    self.on_match(j, j + nd, node.term)
            if n < k:
                need = self._die(node, j, self.d)
                if need is not None:
                    return need

    def _die(self, node: _Node, j: int, d: int) -> _Need | None:
        """The candidate cannot grow: fall back to the longest shorter one."""
        state: tuple[int, _Node] | None = None
        if d > _ANCHOR:
            inner = self.m.scan(node.k)
            if inner is not self.scan and inner.front < d:
                return (node.k, d)
            state = inner.state_at(d)
        self.m.ticks += 1
        if state is None:
            self.node = None
            self.c = j + d - (_ANCHOR - 1)
            return None
        s, node2 = state
        self.j = self.c = j + s
        self.d = d - s
        self.node = node2
        if self.scan is not None:
            self.scan.open_episode(self.j, j + d)
            self.scan.enter(node2)
        return None


class _SelfScan:
    """The automaton run over ``forms[k][1:]``, as far as anyone asked.

    Records (a) the forms that occur inside this form, by end position, and
    (b) for every depth ``D`` the automaton state after ``forms[k][1:D]`` when
    it is at least ``_ANCHOR`` deep - the failure function of ``forms[k][:D]``.
    """

    __slots__ = (
        "_walker",
        "e_ends",
        "e_nodes",
        "e_reach",
        "e_start",
        "k",
        "m_ends",
        "m_recs",
    )

    def __init__(self, matcher: SecretMatcher, k: int) -> None:
        self.k = k
        self.m_ends: list[int] = []
        self.m_recs: list[tuple[int, int, int]] = []
        self.e_start: list[int] = []
        self.e_reach: list[int] = []
        self.e_nodes: list[list[_Node]] = []
        self.e_ends: list[list[int]] = []
        self._walker = _Walker(matcher, matcher.forms[k], 1, self._match, self)

    @property
    def front(self) -> int:
        return self._walker.front

    def _match(self, start: int, end: int, form: int) -> None:
        self.m_ends.append(end)
        self.m_recs.append((start, end, form))

    def open_episode(self, start: int, reach: int) -> None:
        self.e_start.append(start)
        self.e_reach.append(reach)
        self.e_nodes.append([])
        self.e_ends.append([])

    def enter(self, node: _Node) -> None:
        self.e_nodes[-1].append(node)
        self.e_ends[-1].append(node.end)

    def advance(self, depth: int) -> _Need | None:
        """Scan up to ``depth``. None: the records are complete up to there.
        Otherwise the scan that must advance first, to a smaller depth."""
        walker = self._walker
        limit = min(depth, len(walker.text))
        if walker.front >= limit:
            return None
        need = walker.run(limit)
        if need is None:
            if walker.front < limit:
                raise RedactionStallError("self-scan stopped short")
        elif need[1] >= depth:
            raise RedactionStallError("self-scan asked for a deeper scan")
        return need

    def state_at(self, depth: int) -> tuple[int, _Node] | None:
        """``(start, node)`` of the longest proper suffix of ``forms[k][:depth]``
        that is a form prefix of at least ``_ANCHOR`` bytes, or None."""
        i = bisect_left(self.e_reach, depth)
        if i == len(self.e_reach):
            return None
        start = self.e_start[i]
        if start > depth - _ANCHOR:
            return None
        ends = self.e_ends[i]
        return start, self.e_nodes[i][bisect_left(ends, depth - start)]


def _bytes_lcp(a: bytes, b: bytes) -> int:
    return _lcp(a, 0, memoryview(b), 0, min(len(a), len(b)))


class SecretMatcher:
    """One job's secrets, compiled once and shared by every stream (module
    docstring). Build it off the event loop (``asyncio.to_thread``): encoding
    256 KiB of values takes tens of milliseconds.

    Immutable to its users; the lazily filled failure records are guarded by
    :attr:`lock`.
    """

    def __init__(self, secrets: Mapping[str, bytes]) -> None:
        owner: dict[bytes, str] = {}
        for name in sorted(secrets):
            for form in encoded_forms(secrets[name]):
                owner.setdefault(form, name)
        self.names: Final = tuple(sorted(secrets))
        self.forms: Final = sorted(owner)
        self.owners: Final = tuple(owner[form] for form in self.forms)
        self.marks: Final = tuple(marker(name) for name in self.owners)
        self.views: Final = tuple(memoryview(form) for form in self.forms)
        self.max_len: Final = max((len(form) for form in self.forms), default=0)
        self.short: Final = frozenset(
            form[:i] for form in self.forms for i in range(1, _ANCHOR)
        )
        self.root: Final = _Node(0, 0, 0, -1)
        self.root.children = self._build(0, len(self.forms), 0)
        self.anchor: Final = re.compile(
            self._anchor_regex(sorted({form[:_ANCHOR] for form in self.forms}), 0)
            if self.forms
            else rb"(?!)",
            re.DOTALL,
        )
        pairs: dict[str, str] = {}
        for form, name in zip(self.forms, self.owners, strict=True):
            try:
                pairs.setdefault(form.decode("utf-8"), name)
            except UnicodeDecodeError:
                continue
        self.text_pairs: Final = tuple(pairs.items())
        """Forms that are valid text, for :class:`ActiveSecrets`."""
        self.lock: Final = threading.Lock()
        self._scans: dict[int, _SelfScan] = {}
        self.ticks = 0
        """Steps forward made by any walker (the progress witness of
        :meth:`ensure`)."""
        self.stalled = False
        """A loop guard fired: every stream of this matcher withholds output."""

    def __bool__(self) -> bool:
        return bool(self.forms)

    def _build(self, lo: int, hi: int, depth: int) -> dict[int, _Node]:
        """Children of the node whose prefix (``depth`` bytes) the sorted forms
        ``lo..hi`` share and all extend."""
        forms = self.forms
        children: dict[int, _Node] = {}
        i = lo
        while i < hi:
            byte = forms[i][depth]
            j = i + 1
            while j < hi and forms[j][depth] == byte:
                j += 1
            first = forms[i]
            end = len(first) if j - i == 1 else _bytes_lcp(first, forms[j - 1])
            is_form = len(first) == end
            node = _Node(i, depth, end, i if is_form else -1)
            node.children = self._build(i + 1 if is_form else i, j, end)
            children[byte] = node
            i = j
        return children

    def _anchor_regex(self, anchors: list[bytes], depth: int) -> bytes:
        if depth == _ANCHOR:
            return b""
        parts: list[bytes] = []
        i = 0
        while i < len(anchors):
            byte = anchors[i][depth]
            j = i + 1
            while j < len(anchors) and anchors[j][depth] == byte:
                j += 1
            parts.append(
                re.escape(bytes([byte])) + self._anchor_regex(anchors[i:j], depth + 1)
            )
            i = j
        return parts[0] if len(parts) == 1 else b"(?:" + b"|".join(parts) + b")"

    def scan(self, k: int) -> _SelfScan:
        scan = self._scans.get(k)
        if scan is None:
            scan = self._scans[k] = _SelfScan(self, k)
        return scan

    def ensure(self, need: _Need) -> None:
        """Advance a self-scan, and first whatever it needs (an explicit
        stack; module docstring, "Termination").

        ``idle`` counts the turns in a row in which no walker stepped
        (``ticks`` unchanged): a push whose scan needed another scan first,
        or an ``advance`` that returned None without running (``front`` was
        at its limit already). Depths are ``> 4`` and strictly shrink up the
        stack, so it holds at most ``need[1] - 4`` needs, and a pop follows a
        run that stepped; a right matcher is therefore idle for at most
        ``need[1] - 5`` turns in a row. The budget is ``need[1] + 2``: more
        idle turns than that is a bug and raises instead of spinning."""
        stack = [need]
        seen = self.ticks
        idle = 0
        while stack:
            k, depth = stack[-1]
            more = self.scan(k).advance(depth)
            if self.ticks != seen:
                seen = self.ticks
                idle = 0
            else:
                idle += 1
                if idle > need[1] + 2:
                    raise RedactionStallError("self-scans wait for each other")
            if more is None:
                stack.pop()
            else:
                stack.append(more)


class StreamRedactor:
    """Redact one byte stream fed in chunks (module docstring).

    ``secrets`` is a :class:`SecretMatcher` (shared; nothing is compiled here)
    or a plain mapping (compiled here - tests and one-shot use).
    """

    def __init__(
        self,
        secrets: SecretMatcher | Mapping[str, bytes],
        *,
        length_preserving: bool = False,
    ) -> None:
        self._m = (
            secrets if isinstance(secrets, SecretMatcher) else SecretMatcher(secrets)
        )
        self._preserve = length_preserving
        self._buf = bytearray()
        # Merged matched intervals not fully emitted yet, in ``_buf``
        # coordinates: [start, end, form, already started].
        self._iv: list[list[int]] = []
        self._walker = _Walker(self._m, self._buf, 0, self._match)
        self.replaced = 0
        """Replacements made so far (tests and metrics; never the values)."""
        self.failed = False
        """A loop guard fired (:class:`RedactionStallError`): from then on
        this stream emits ``*`` for every byte - nothing can leak, nothing can
        hang."""

    @property
    def held(self) -> int:
        """Bytes kept back for the next chunk."""
        return len(self._buf)

    def _match(self, start: int, end: int, form: int) -> None:
        # Matches arrive by end position; one that starts first (or, at the
        # same start, is longer) names the merged interval.
        iv = self._iv
        while iv and iv[-1][0] >= start:
            iv.pop()
        if iv and iv[-1][1] > start:
            iv[-1][1] = max(iv[-1][1], end)
        else:
            iv.append([start, end, form, 0])

    def _replacement(self, item: list[int], lo: int, hi: int) -> bytes:
        start, end, form, begun = item
        mark = self._m.marks[form]
        if not begun:
            item[3] = 1
            self.replaced += 1
        if not self._preserve:
            return b"" if begun else mark
        if end - start <= len(mark):
            return b"*" * (hi - lo)
        a, b = lo - start, hi - start
        return mark[a:b] + b"*" * (b - max(a, len(mark)))

    def _emit(self, upto: int) -> bytes:
        """Release ``_buf[:upto]`` (less if an interval's shape is undecided)."""
        buf = self._buf
        out: list[bytes | bytearray] = []
        pos = 0
        done = 0
        for item in self._iv:
            start, end = item[0], item[1]
            if start >= upto:
                break
            if (
                end > upto
                and self._preserve
                and end - start <= len(self._m.marks[item[2]])
            ):
                # Still growing and not yet longer than its marker: whether it
                # is "marker + stars" or "stars" is not known. Keep it.
                upto = start
                break
            if start > pos:
                out.append(buf[pos:start])
            hi = min(end, upto)
            out.append(self._replacement(item, max(start, pos), hi))
            pos = hi
            if end > upto:
                break
            done += 1
        del self._iv[:done]
        if pos < upto:
            out.append(buf[pos:upto])
        if upto:
            del buf[:upto]
            for item in self._iv:
                item[0] -= upto
                item[1] -= upto
            walker = self._walker
            walker.c -= upto
            walker.j -= upto
            walker.front -= upto
        return b"".join(out)

    def _read(self, end: int) -> None:
        """Run the walker over ``_buf[:end]``, advancing the self-scans it
        waits for."""
        m = self._m
        walker = self._walker
        waited = walker.key() - 1
        while (need := walker.run(end)) is not None:
            # Between two waits the walker took the step it waited for, so
            # its measure went up.
            if walker.key() <= waited:
                raise RedactionStallError("stream waits twice in place")
            waited = walker.key()
            m.ensure(need)

    def feed(self, data: bytes) -> bytes:
        """The redacted bytes that are safe to emit now."""
        m = self._m
        if not m:
            return data
        with m.lock:
            buf = self._buf
            buf += data
            if m.stalled:
                return self._withhold()
            walker = self._walker
            end = len(buf)
            try:
                self._read(end)
            except RedactionStallError:
                m.stalled = True
                return self._withhold()
            if walker.node is None:
                # Nothing in progress: keep only the longest tail that could
                # begin a form (T-3).
                for start in range(max(walker.c, 0), end):
                    if bytes(buf[start:end]) in m.short:
                        walker.c = start
                        break
                else:
                    walker.c = end
            return self._emit(walker.c)

    def _withhold(self) -> bytes:
        """Fail closed: everything held becomes ``*`` (same length)."""
        self.failed = True
        out = b"*" * len(self._buf)
        self._buf.clear()
        self._iv.clear()
        walker = self._walker
        walker.node = None
        walker.c = walker.j = walker.d = walker.front = 0
        return out

    def flush(self) -> bytes:
        """Release what is held (the end of the stream, or a gap)."""
        m = self._m
        if not m:
            return b""
        with m.lock:
            if m.stalled:
                return self._withhold()
            out = self._emit(len(self._buf))
            self._buf.clear()
            self._iv.clear()
            walker = self._walker
            walker.node = None
            walker.c = walker.j = walker.d = walker.front = 0
            return out


def redact_bytes(
    data: bytes,
    secrets: SecretMatcher | Mapping[str, bytes],
    *,
    length_preserving: bool = False,
) -> bytes:
    """One-shot :class:`StreamRedactor` over a whole buffer."""
    redactor = StreamRedactor(secrets, length_preserving=length_preserving)
    return redactor.feed(data) + redactor.flush()


class ActiveSecrets:
    """Values in use in this process, by holder (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_holder: dict[str, SecretMatcher] = {}
        self._text: list[tuple[str, str]] = []

    def add(self, holder: str, secrets: SecretMatcher | Mapping[str, bytes]) -> None:
        """Register a holder's values. Pass the attempt's :class:`SecretMatcher`
        (built off the event loop) - a plain mapping is compiled here."""
        if not secrets:
            return
        matcher = (
            secrets if isinstance(secrets, SecretMatcher) else SecretMatcher(secrets)
        )
        with self._lock:
            self._by_holder[holder] = matcher
            self._rebuild()

    def remove(self, holder: str) -> None:
        with self._lock:
            if self._by_holder.pop(holder, None) is not None:
                self._rebuild()

    def remove_prefix(self, prefix: str) -> None:
        """Remove every holder whose id starts with ``prefix``."""
        with self._lock:
            gone = [h for h in self._by_holder if h.startswith(prefix)]
            for holder in gone:
                del self._by_holder[holder]
            if gone:
                self._rebuild()

    def __bool__(self) -> bool:
        return bool(self._text)

    def _rebuild(self) -> None:
        pairs: dict[str, str] = {}
        for matcher in self._by_holder.values():
            for text, name in matcher.text_pairs:
                pairs.setdefault(text, name)
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
            return sorted({n for m in self._by_holder.values() for n in m.names})


ACTIVE: Final = ActiveSecrets()


def scrub_active(text: str, *, placeholder: bool = False) -> str:
    """:data:`ACTIVE`'s values (and encodings) out of ``text``."""
    return ACTIVE.scrub(text, placeholder=placeholder) if ACTIVE else text


_PH_OPEN: Final = ""
_PH_CLOSE: Final = ""
_PH_RE: Final = re.compile(f"{_PH_OPEN}([A-Z0-9_]{{1,64}}){_PH_CLOSE}")


def restore_markers(text: str) -> str:
    """Placeholders left by ``scrub_active(..., placeholder=True)`` ->
    ``[secret:NAME]``."""
    if _PH_OPEN not in text:
        return text
    return _PH_RE.sub(lambda m: f"[secret:{m.group(1)}]", text)
