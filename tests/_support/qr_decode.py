"""A small, independent QR-code decoder for tests (V0.8 G5, D199).

The dashboard draws an invite's join code as a QR code with the npm library
``qrcode-generator``. To prove the picture really says the code - not just
"the same library drew the same thing twice" - the e2e test reads the module
matrix back from the page's SVG and decodes it here, from the QR standard
(ISO/IEC 18004), with nothing shared with the encoder:

* format information (ECC level + mask) with its BCH check;
* the function patterns of versions 1-40 (finders, timing, alignment,
  format and version areas);
* mask removal, the zig-zag codeword order, block de-interleaving;
* every block's Reed-Solomon check symbols are RE-COMPUTED and must match
  (a wrong table or a misread module cannot pass silently);
* byte-mode segments (what the dashboard emits).

Clean module matrices only (no image processing): ``matrix[row][col]`` is
``True`` for a dark module. Error correction level M only (the dashboard's).
"""

from __future__ import annotations

from typing import Final

__all__ = ["QrDecodeError", "decode_matrix", "matrix_from_svg_path"]


class QrDecodeError(ValueError):
    """The matrix is not a valid QR code this decoder understands."""


# ECC level M: check symbols per block and number of blocks, versions 1-40
# (index 0 unused).
_ECC_PER_BLOCK_M: Final = (
    -1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26,
    26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28,
)  # fmt: skip
_BLOCKS_M: Final = (
    -1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
    17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49,
)  # fmt: skip
_ECC_LEVEL_BITS: Final = {1: "L", 0: "M", 3: "Q", 2: "H"}
_FORMAT_XOR: Final = 0x5412


def _bit(value: int, index: int) -> bool:
    return (value >> index) & 1 == 1


def _format_bch(data5: int) -> int:
    rem = data5
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data5 << 10 | rem) ^ _FORMAT_XOR


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    size = 17 + 4 * version
    count = version // 7 + 2
    step = 26 if version == 32 else -(-(version * 4 + 4) // (count * 2 - 2)) * 2
    positions = [6]
    pos = size - 7
    while len(positions) < count:
        positions.insert(1, pos)
        pos -= step
    return positions


def _raw_data_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def _function_mask(version: int) -> list[list[bool]]:
    size = 17 + 4 * version
    fn = [[False] * size for _ in range(size)]

    def mark(x: int, y: int) -> None:
        if 0 <= x < size and 0 <= y < size:
            fn[y][x] = True

    for i in range(size):  # timing patterns
        mark(6, i)
        mark(i, 6)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                mark(cx + dx, cy + dy)
    align = _alignment_positions(version)
    last = len(align) - 1
    for i, ay in enumerate(align):
        for j, ax in enumerate(align):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    mark(ax + dx, ay + dy)
    for i in range(9):  # format areas (both copies) + the dark module
        mark(8, i)
        mark(i, 8)
    for i in range(8):
        mark(size - 1 - i, 8)
        mark(8, size - 1 - i)
    if version >= 7:  # version information areas
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            mark(a, b)
            mark(b, a)
    return fn


def _read_format(m: list[list[bool]]) -> tuple[str, int]:
    def first_copy() -> int:
        bits = 0
        for i in range(6):
            bits |= m[i][8] << i
        bits |= m[7][8] << 6
        bits |= m[8][8] << 7
        bits |= m[8][7] << 8
        for i in range(9, 15):
            bits |= m[8][14 - i] << i
        return bits

    def second_copy() -> int:
        size = len(m)
        bits = 0
        for i in range(8):
            bits |= m[8][size - 1 - i] << i
        for i in range(8, 15):
            bits |= m[size - 15 + i][8] << i
        return bits

    for bits in (first_copy(), second_copy()):
        data5 = (bits ^ _FORMAT_XOR) >> 10
        if _format_bch(data5) == bits:
            return _ECC_LEVEL_BITS[data5 >> 3], data5 & 7
    raise QrDecodeError("format information fails its BCH check in both copies")


def _masked(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _gf_mul(a: int, b: int) -> int:
    product = 0
    for i in range(7, -1, -1):
        product = (product << 1) ^ ((product >> 7) * 0x11D)
        product ^= ((b >> i) & 1) * a
    return product & 0xFF


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    divisor = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _gf_mul(root, 0x02)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i in range(degree):
            result[i] ^= _gf_mul(divisor[i], factor)
    return result


def decode_matrix(matrix: list[list[bool]]) -> str:
    """The text of a clean QR module matrix (byte mode, ECC M)."""
    size = len(matrix)
    if size < 21 or (size - 17) % 4 != 0 or any(len(r) != size for r in matrix):
        raise QrDecodeError(f"a {size}-module square is not a QR code size")
    version = (size - 17) // 4
    level, mask = _read_format(matrix)
    if level != "M":
        raise QrDecodeError(f"only ECC level M is supported, the code says {level}")
    fn = _function_mask(version)
    if not matrix[size - 8][8]:
        raise QrDecodeError("the always-dark module is light")

    raw = _raw_data_modules(version) // 8
    bits: list[int] = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and len(bits) < raw * 8:
                    bits.append(int(matrix[y][x] ^ _masked(mask, x, y)))
        right -= 2
    if len(bits) != raw * 8:
        raise QrDecodeError("fewer data modules than the version holds")
    codewords = [
        int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)
    ]

    blocks = _BLOCKS_M[version]
    ecc = _ECC_PER_BLOCK_M[version]
    short_len = raw // blocks
    short_count = blocks - raw % blocks
    data_lens = [short_len - ecc + (0 if b < short_count else 1) for b in range(blocks)]
    data_blocks: list[list[int]] = [[] for _ in range(blocks)]
    ecc_blocks: list[list[int]] = [[] for _ in range(blocks)]
    k = 0
    for i in range(max(data_lens)):
        for b in range(blocks):
            if i < data_lens[b]:
                data_blocks[b].append(codewords[k])
                k += 1
    for _ in range(ecc):
        for b in range(blocks):
            ecc_blocks[b].append(codewords[k])
            k += 1
    for b in range(blocks):
        if _rs_remainder(data_blocks[b], ecc) != ecc_blocks[b]:
            raise QrDecodeError(f"block {b}: Reed-Solomon check symbols do not match")

    stream = "".join(format(byte, "08b") for block in data_blocks for byte in block)
    pos = 0
    out = bytearray()
    while pos + 4 <= len(stream):
        mode = int(stream[pos : pos + 4], 2)
        pos += 4
        if mode == 0:
            break
        if mode != 0b0100:
            raise QrDecodeError(f"segment mode {mode:04b} is not byte mode")
        count_bits = 8 if version <= 9 else 16
        count = int(stream[pos : pos + count_bits], 2)
        pos += count_bits
        if pos + 8 * count > len(stream):
            raise QrDecodeError("a byte segment runs past the data")
        for _ in range(count):
            out.append(int(stream[pos : pos + 8], 2))
            pos += 8
    return out.decode("latin-1")


def matrix_from_svg_path(d: str, modules: int, margin: int) -> list[list[bool]]:
    """The module matrix drawn by the dashboard's ``M{x} {y}h1v1h-1z`` path."""
    import re

    matrix = [[False] * modules for _ in range(modules)]
    for x_text, y_text in re.findall(r"M(\d+) (\d+)h1v1h-1z", d):
        x, y = int(x_text) - margin, int(y_text) - margin
        if not (0 <= x < modules and 0 <= y < modules):
            raise QrDecodeError(f"a square at ({x}, {y}) lies in the quiet zone")
        matrix[y][x] = True
    return matrix
