# ruff: noqa: E501, S311, N818, PERF401, RUF005
"""Tester r3 (V0.7 G6): an independent brute-force oracle for the redactor (support module).

Written from the module's documented contract, not from its code:

* forms of a value = raw, base64 std/urlsafe padded/unpadded, hex lower/upper,
  percent (upper/lower hex, ``%20`` or ``+`` for space); forms < 4 bytes unused;
* every occurrence of every form is matched; the UNION of matched intervals
  is replaced (intervals that only touch stay separate);
* agent style: one ``[secret:NAME]`` per merged interval, named after the match
  that starts first (the longest if several), nothing else changes;
* controller style: same length as the input; marker padded with ``*`` (only
  ``*`` if the interval is not longer than the marker).

Run long with ``python tests/unit/common/test_v07_g6_tester_r3_redaction_oracle.py
SEED SECONDS`` under ``PYTHONHASHSEED=SEED`` (see ``soak``).
"""

from __future__ import annotations

import base64
import random
import signal
import time

from meshforge.common.secret_redaction import SecretMatcher, StreamRedactor

FEED_LIMIT_S = 2


class FeedTooSlow(Exception):
    pass


def _alarm(_sig: int, _frame: object) -> None:
    raise FeedTooSlow


def _pct(value: bytes, *, upper: bool, plus: bool) -> bytes:
    safe = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")
    out = b""
    for b in value:
        if b == 0x20 and plus:
            out += b"+"
        elif b in safe:
            out += bytes([b])
        else:
            out += (b"%%%02X" if upper else b"%%%02x") % b
    return out


def forms_of(value: bytes) -> set[bytes]:
    f = {value}
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        p = enc(value)
        f |= {p, p.rstrip(b"=")}
    f |= {value.hex().encode(), value.hex().upper().encode()}
    for upper in (True, False):
        for plus in (False, True):
            f.add(_pct(value, upper=upper, plus=plus))
    return {x for x in f if len(x) >= 4}


def oracle(secrets: dict[str, bytes], data: bytes, *, preserve: bool) -> bytes:
    owner: dict[bytes, str] = {}
    for name in sorted(secrets):
        for f in forms_of(secrets[name]):
            owner.setdefault(f, name)
    # matches: (start, end, name)
    matches = []
    for f, name in owner.items():
        i = data.find(f)
        while i != -1:
            matches.append((i, i + len(f), name))
            i = data.find(f, i + 1)
    matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    merged: list[list] = []  # [start, end, name]
    for s, e, n in matches:
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, n])
    out = b""
    pos = 0
    for s, e, n in merged:
        out += data[pos:s]
        mark = f"[secret:{n}]".encode()
        if preserve:
            out += (
                b"*" * (e - s)
                if e - s <= len(mark)
                else mark + b"*" * (e - s - len(mark))
            )
        else:
            out += mark
        pos = e
    return out + data[pos:]


POOL = [
    b"0",
    b"a",
    b"3",
    b"6",
    b"1",
    b"%",
    b"+",
    b"/",
    b"=",
    b"_",
    b"-",
    b" ",
    b"A",
    b"\n",
    b"\xff",
    b"2",
    b"b",
]
HEXY = [b"0", b"a", b"3", b"6", b"1"]


def gen_secrets(rnd: random.Random) -> dict[str, bytes]:
    kind = rnd.random()
    if kind < 0.35:
        alpha = rnd.sample(HEXY, rnd.choice((2, 3)))
    elif kind < 0.7:
        alpha = rnd.sample(POOL, rnd.choice((2, 3)))
    else:
        alpha = rnd.sample(POOL, 2) + rnd.sample([b"3061", b"%2B", b"0a"], 1)
    out: dict[str, bytes] = {}
    for i in range(rnd.randint(1, 6)):
        n = rnd.randint(4, 14)
        style = rnd.random()
        if style < 0.2:
            unit = b"".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 3)))
            v = (unit * 20)[:n]
        elif style < 0.3 and out:
            # a substring / extension of another secret or of one of its forms
            base = rnd.choice(list(out.values()))
            fs = sorted(forms_of(base))
            src = rnd.choice(fs + [base])
            a = rnd.randint(0, max(0, len(src) - 4))
            v = src[a : a + rnd.randint(4, 14)]
            if len(v) < 4:
                v = (v + b"0000")[:4]
        else:
            v = b"".join(rnd.choice(alpha) for _ in range(n))
        out[f"S{i}"] = v
    return out


def gen_secrets_long(rnd: random.Random) -> dict[str, bytes]:
    """Long, periodic and nested values: failure chains as deep as they get."""
    alpha = rnd.sample(
        [b"a", b"b", b"0", b"3", b"6", b"1", b"%", b" "], rnd.choice((1, 2, 3))
    )
    unit = b"".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 4)))
    out: dict[str, bytes] = {}
    for i in range(rnd.randint(1, 6)):
        mode = rnd.random()
        n = rnd.choice((4, 5, 8, 16, 33, 64, 100, 257, 400))
        if mode < 0.5:
            v = (unit * (n // len(unit) + 1))[:n]
            if rnd.random() < 0.4:
                v = v[:-1] + rnd.choice(alpha)
        elif mode < 0.75 and out:
            base = rnd.choice(list(out.values()))
            src = rnd.choice(sorted(forms_of(base)) + [base])
            v = src[rnd.randint(0, len(src) // 2) :][: max(4, n)]
        else:
            v = b"".join(rnd.choice(alpha) for _ in range(n))
        if len(v) < 4:
            v = (v * 4)[:4]
        out[f"S{i}"] = v
    return out


def gen_stream(rnd: random.Random, secrets: dict[str, bytes]) -> bytes:
    fs = sorted({f for v in secrets.values() for f in forms_of(v)})
    vals = list(secrets.values())
    alpha = sorted({b for v in vals for b in v}) or [48]
    data = bytearray()
    for _ in range(rnd.randint(1, 14)):
        r = rnd.random()
        if r < 0.3:
            data += rnd.choice(fs)
        elif r < 0.5:
            f = rnd.choice(fs)
            a = rnd.randint(0, len(f) - 1)
            b = rnd.randint(a + 1, len(f))
            data += f[a:b]
        elif r < 0.75:
            # overlap: find a form whose prefix equals a suffix of data
            cands = []
            for f in fs:
                for k in range(1, min(len(f), len(data)) + 1):
                    if data[-k:] == f[:k]:
                        cands.append(f[k:])
            data += rnd.choice(cands) if cands else rnd.choice(fs)
        elif r < 0.85:
            f = bytearray(rnd.choice(fs))
            f[rnd.randrange(len(f))] = rnd.choice(alpha)
            data += f
        elif r < 0.92:
            data += rnd.choice(fs) * rnd.randint(2, 4)
        else:
            data += bytes(rnd.choice(alpha) for _ in range(rnd.randint(1, 6)))
    if rnd.random() < 0.3:
        data = data[: rnd.randint(0, len(data))]
    return bytes(data)


def chunkings(rnd: random.Random, n: int) -> list[list[int]]:
    cuts = [list(range(1, n))]  # one-byte chunks
    for _ in range(3):
        k = rnd.randint(0, min(n, 8))
        cuts.append(
            sorted(rnd.sample(range(1, n), min(k, max(0, n - 1)))) if n > 1 else []
        )
    cuts.append([])
    return cuts


def run_stream(matcher: SecretMatcher, data: bytes, cuts: list[int], *, preserve: bool):
    r = StreamRedactor(matcher, length_preserving=preserve)
    out = b""
    prev = 0
    worst = 0.0
    for c in [*cuts, len(data)]:
        t = time.perf_counter()
        signal.setitimer(signal.ITIMER_REAL, FEED_LIMIT_S)
        try:
            out += r.feed(data[prev:c])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        worst = max(worst, time.perf_counter() - t)
        prev = c
    signal.setitimer(signal.ITIMER_REAL, FEED_LIMIT_S)
    try:
        out += r.flush()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return out, r.failed, worst


class Stats:
    def __init__(self) -> None:
        self.matchers = self.streams = self.feeds = 0
        self.bad: list[str] = []
        self.slow = self.stall = 0
        self.worst_feed = 0.0

    def line(self) -> str:
        return (
            f"matchers={self.matchers} streams={self.streams} feeds={self.feeds} "
            f"bad={len(self.bad)} slow={self.slow} stalled={self.stall} "
            f"worst_feed={self.worst_feed * 1000:.1f}ms"
        )


def one_case(rnd: random.Random, st: Stats, *, long: bool = False) -> None:
    secrets = gen_secrets_long(rnd) if long else gen_secrets(rnd)
    matcher = SecretMatcher(secrets)
    st.matchers += 1
    for _ in range(4):
        data = gen_stream(rnd, secrets)
        for preserve in (False, True):
            want = oracle(secrets, data, preserve=preserve)
            for cuts in chunkings(rnd, len(data)):
                st.streams += 1
                st.feeds += len(cuts) + 2
                try:
                    got, failed, worst = run_stream(
                        matcher, data, cuts, preserve=preserve
                    )
                except FeedTooSlow:
                    st.slow += 1
                    st.bad.append(f"SLOW {secrets!r} {data!r} {cuts}")
                    matcher = SecretMatcher(secrets)
                    continue
                st.worst_feed = max(st.worst_feed, worst)
                if failed or matcher.stalled:
                    st.stall += 1
                    st.bad.append(f"STALL {secrets!r} {data!r} {cuts}")
                    matcher = SecretMatcher(secrets)
                elif got != want:
                    st.bad.append(
                        f"DIFF {secrets!r} {data!r} cuts={cuts} preserve={preserve}\n got={got!r}\nwant={want!r}"
                    )
                if preserve and len(got) != len(data):
                    st.bad.append(f"LEN {secrets!r} {data!r}")


def soak(seed: int, seconds: float, *, long: bool = False) -> Stats:
    signal.signal(signal.SIGALRM, _alarm)
    rnd = random.Random(seed)
    st = Stats()
    end = time.time() + seconds
    while time.time() < end and len(st.bad) < 20:
        one_case(rnd, st, long=long)
    return st


CASES: dict[str, dict[str, bytes]] = {
    "t17": {"S0": b"00aaaa000a", "S1": b"a000a"},
    "leak": {"S0": b"aaa aaa  ", "S1": b"aa  ", "S2": b" aaa  a"},
    "dense": {"S0": b"0a0a0a0a", "S1": b"a0a0", "S2": b"3061", "S3": b"%2B+%2B"},
}

MAIN = r"""ENC = {'t17': ['66656665636463646364636466656665666563', '6665666563646364636463646665666566656364', '65653434343465656534', '3465656534', '2d66656665636463646364636466656665666563343434', '63646364636466656665666563', '346565653465653434343465656534', '656534343434656565343465656534', '66656665636463646364636466656665666563646364636466656665'], 'leak': ['63646364636467656364636463646765676563646364636467656765', '3434347534343475753434757575343434757534', '343434753434347575', '6364636463646765', '6765636463646364676567656364', '3434757575343434757534'], 'dense': ['653465346534653465346534', '3465346534653465', '6665636466656364', '7067177e7067177067177e706717', '66656364666563646665636466656364666563', '63646665636466656364']}

import os, sys, time
case = sys.argv[1]
payloads = [bytes(x ^ 0x55 for x in bytes.fromhex(h)) for h in ENC[case]]
out = sys.stdout.buffer
for i in range(40):
    for p in payloads:
        out.write(p + (b"\n" if i % 3 == 0 else b" "));
        if i % 5 == 0:
            out.flush()
    out.flush(); time.sleep(0.01)
    # a one-byte-at-a-time burst
    for b in payloads[i % len(payloads)]:
        out.write(bytes([b])); out.flush()
    out.write(b"\n"); out.flush()
sys.stderr.write("done\n")
"""
