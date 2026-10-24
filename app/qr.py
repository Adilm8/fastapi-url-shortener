"""
Self-contained QR Code visual matrix and image generator.

Implements ISO/IEC 18004 Byte-mode QR Code matrix generation with finder patterns,
timing patterns, alignment patterns, format information, and Reed-Solomon error correction
without any external network calls, using only the Python standard library and Pillow.
"""

from __future__ import annotations

import io
from typing import Optional
from PIL import Image, ImageDraw

# Galois Field GF(2^8) lookup tables with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D = 285)
_EXP_TABLE = [0] * 512
_LOG_TABLE = [0] * 256

_x = 1
for _i in range(255):
    _EXP_TABLE[_i] = _x
    _LOG_TABLE[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D

for _i in range(255, 512):
    _EXP_TABLE[_i] = _EXP_TABLE[_i - 255]


def _gf_mul(x: int, y: int) -> int:
    """Multiply two numbers in GF(2^8)."""
    if x == 0 or y == 0:
        return 0
    return _EXP_TABLE[_LOG_TABLE[x] + _LOG_TABLE[y]]


def _rs_generator_poly(degree: int) -> list[int]:
    """Generate Reed-Solomon error-correction generator polynomial of given degree."""
    g = [1]
    for i in range(degree):
        root = _EXP_TABLE[i]
        new_g = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            new_g[j] ^= c
            new_g[j + 1] ^= _gf_mul(c, root)
        g = new_g
    return g


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    """Compute Reed-Solomon error-correction codewords for the given data bytes."""
    gen = _rs_generator_poly(ec_len)
    msg = list(data) + [0] * ec_len
    for i in range(len(data)):
        lead = msg[i]
        if lead != 0:
            for j in range(1, len(gen)):
                msg[i + j] ^= _gf_mul(gen[j], lead)
    return msg[len(data):]


# Version specifications for Error Correction Level L
# Format: {version: {'size': int, 'data': int, 'ec': int, 'blocks': list[tuple[count, data_len, ec_len]], 'align': list[int]}}
_VERSIONS: dict[int, dict] = {
    1: {"size": 21, "data": 19, "ec": 7, "blocks": [(1, 19, 7)], "align": []},
    2: {"size": 25, "data": 34, "ec": 10, "blocks": [(1, 34, 10)], "align": [6, 18]},
    3: {"size": 29, "data": 55, "ec": 15, "blocks": [(1, 55, 15)], "align": [6, 22]},
    4: {"size": 33, "data": 80, "ec": 20, "blocks": [(1, 80, 20)], "align": [6, 26]},
    5: {"size": 37, "data": 108, "ec": 26, "blocks": [(1, 108, 26)], "align": [6, 30]},
    6: {"size": 41, "data": 136, "ec": 36, "blocks": [(2, 68, 18)], "align": [6, 34]},
    7: {"size": 45, "data": 156, "ec": 40, "blocks": [(2, 78, 20)], "align": [6, 22, 38]},
    8: {"size": 49, "data": 194, "ec": 48, "blocks": [(2, 97, 24)], "align": [6, 24, 42]},
    9: {"size": 53, "data": 232, "ec": 60, "blocks": [(2, 116, 30)], "align": [6, 26, 46]},
    10: {"size": 57, "data": 274, "ec": 72, "blocks": [(2, 68, 18), (2, 69, 18)], "align": [6, 28, 50]},
}


def _pick_version(data_byte_count: int) -> int:
    """Select the smallest QR version that fits data_byte_count bytes in Byte mode."""
    for v in range(1, 11):
        cap = _VERSIONS[v]["data"]
        header_len = 2 if v < 10 else 3
        if data_byte_count + header_len <= cap:
            return v
    raise ValueError(
        f"QR payload of {data_byte_count} bytes exceeds the maximum supported capacity (Version 10-L)."
    )


def _build_codewords(data: str, version: int) -> list[int]:
    """Encode string data into interleaved data and error-correction codewords."""
    info = _VERSIONS[version]
    total_data_bytes = info["data"]
    raw_data = data.encode("utf-8")
    header_bytes = 2 if version < 10 else 3
    if len(raw_data) + header_bytes > total_data_bytes:
        raise ValueError(f"QR payload does not fit in Version {version}.")

    # Mode 0100 (Byte mode: 4 bits)
    bits = [0, 1, 0, 0]

    # Character count indicator (8 bits for V1-V9, 16 bits for V10)
    length_bits = 8 if version < 10 else 16
    for i in range(length_bits - 1, -1, -1):
        bits.append((len(raw_data) >> i) & 1)

    # Data bytes (8 bits each)
    for byte in raw_data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    # Terminator (up to 4 zeros)
    max_bits = total_data_bytes * 8
    terminator_len = min(4, max_bits - len(bits))
    bits.extend([0] * terminator_len)

    # Pad to byte boundary
    if len(bits) % 8 != 0:
        bits.extend([0] * (8 - (len(bits) % 8)))

    # Convert bitstream to byte list
    data_bytes: list[int] = []
    for i in range(0, len(bits), 8):
        byte_val = 0
        for b in bits[i:i + 8]:
            byte_val = (byte_val << 1) | b
        data_bytes.append(byte_val)

    # Pad with alternating 0xEC and 0x11
    pad_bytes = [0xEC, 0x11]
    pad_idx = 0
    while len(data_bytes) < total_data_bytes:
        data_bytes.append(pad_bytes[pad_idx % 2])
        pad_idx += 1

    # Split into blocks and compute Reed-Solomon EC
    blocks_spec = info["blocks"]
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for num_blocks, data_count, ec_count in blocks_spec:
        for _ in range(num_blocks):
            block = data_bytes[offset:offset + data_count]
            offset += data_count
            ec = _rs_encode(block, ec_count)
            data_blocks.append(block)
            ec_blocks.append(ec)

    # Interleave data codewords
    final_codewords: list[int] = []
    max_data_len = max(len(b) for b in data_blocks)
    for i in range(max_data_len):
        for block in data_blocks:
            if i < len(block):
                final_codewords.append(block[i])

    # Interleave EC codewords
    max_ec_len = max(len(b) for b in ec_blocks)
    for i in range(max_ec_len):
        for ec in ec_blocks:
            if i < len(ec):
                final_codewords.append(ec[i])

    # Convert final codewords into bitstream
    bitstream: list[int] = []
    for cw in final_codewords:
        for i in range(7, -1, -1):
            bitstream.append((cw >> i) & 1)

    return bitstream


def _get_version_info(version: int) -> list[int]:
    """Compute 18-bit version information for Version >= 7 (BCH (18, 6) code)."""
    d = version << 12
    g = 0x1F25
    rem = d
    for i in range(17, 11, -1):
        if (rem >> i) & 1:
            rem ^= (g << (i - 12))
    val = d | rem
    return [(val >> (17 - i)) & 1 for i in range(18)]


def generate_qr_matrix(data: str) -> list[list[int]]:
    """
    Generate a 2D binary matrix (0 for white, 1 for black) for the QR code encoding `data`.
    Follows standard ISO/IEC 18004 specifications with finder patterns, timing tracks,
    alignment patterns, format information, and Mask 0.
    """
    raw_bytes = data.encode("utf-8")
    v = _pick_version(len(raw_bytes))
    info = _VERSIONS[v]
    size = info["size"]

    matrix = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    # Finder patterns
    def set_finder(r: int, c: int) -> None:
        for dr in range(7):
            for dc in range(7):
                reserved[r + dr][c + dc] = True
                if dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4):
                    matrix[r + dr][c + dc] = 1
                else:
                    matrix[r + dr][c + dc] = 0

    set_finder(0, 0)
    set_finder(0, size - 7)
    set_finder(size - 7, 0)

    # Separators
    for i in range(8):
        for r, c in [
            (7, i), (i, 7),
            (7, size - 1 - i), (i, size - 8),
            (size - 8, i), (size - 1 - i, 7)
        ]:
            if 0 <= r < size and 0 <= c < size:
                reserved[r][c] = True
                matrix[r][c] = 0

    # Timing tracks
    for i in range(8, size - 8):
        reserved[6][i] = True
        reserved[i][6] = True
        matrix[6][i] = 1 if i % 2 == 0 else 0
        matrix[i][6] = 1 if i % 2 == 0 else 0

    # Alignment patterns
    align_coords = info["align"]
    for ar in align_coords:
        for ac in align_coords:
            if (ar < 9 and ac < 9) or (ar < 9 and ac >= size - 9) or (ar >= size - 9 and ac < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    reserved[ar + dr][ac + dc] = True
                    if abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0):
                        matrix[ar + dr][ac + dc] = 1
                    else:
                        matrix[ar + dr][ac + dc] = 0

    # Dark module
    reserved[size - 8][8] = True
    matrix[size - 8][8] = 1

    # Format info
    format_bits = [1, 1, 1, 0, 1, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0]

    tl_coords = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)
    ]
    for bit, (r, c) in zip(format_bits, tl_coords):
        reserved[r][c] = True
        matrix[r][c] = bit

    br_coords = [
        (size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
        (size - 5, 8), (size - 6, 8), (size - 7, 8),
        (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
        (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)
    ]
    for bit, (r, c) in zip(format_bits, br_coords):
        reserved[r][c] = True
        matrix[r][c] = bit

    # Version info
    if v >= 7:
        v_bits = _get_version_info(v)
        idx = 0
        for r in range(6):
            for c in range(3):
                bit = v_bits[17 - idx]
                matrix[r][size - 11 + c] = bit
                reserved[r][size - 11 + c] = True
                matrix[size - 11 + c][r] = bit
                reserved[size - 11 + c][r] = True
                idx += 1

    # Data and EC bits
    bits = _build_codewords(data, v)
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
            continue
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not reserved[r][c]:
                    val = bits[bit_idx] if bit_idx < len(bits) else 0
                    bit_idx += 1
                    # Mask 0 condition
                    if (r + c) % 2 == 0:
                        val ^= 1
                    matrix[r][c] = val
        col -= 2
        upward = not upward

    return matrix


def generate_qr_svg(data: str, scale: int = 8, border: int = 2) -> str:
    """
    Generate a scalable vector graphics (SVG) QR code string for the given data.

    :param data: The payload string (URL or text) to encode.
    :param scale: Size in pixels for each module square (default 8).
    :param border: Quiet zone border width in modules (default 2).
    :return: A valid UTF-8 SVG string.
    """
    matrix = generate_qr_matrix(data)
    matrix_size = len(matrix)
    total_size = (matrix_size + 2 * border) * scale

    rects = []
    for r in range(matrix_size):
        for c in range(matrix_size):
            if matrix[r][c]:
                x = (c + border) * scale
                y = (r + border) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{scale}" height="{scale}" fill="black"/>')

    rect_str = "\n  ".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_size} {total_size}" '
        f'width="{total_size}" height="{total_size}" shape-rendering="crispEdges">\n'
        f'  <rect width="100%" height="100%" fill="white"/>\n'
        f'  {rect_str}\n'
        f'</svg>'
    )


def generate_qr_png(data: str, scale: int = 8, border: int = 2) -> bytes:
    """
    Generate raster PNG image bytes for the given data using Pillow.

    :param data: The payload string (URL or text) to encode.
    :param scale: Size in pixels for each module square (default 8).
    :param border: Quiet zone border width in modules (default 2).
    :return: PNG image binary bytes.
    """
    matrix = generate_qr_matrix(data)
    matrix_size = len(matrix)
    dim = matrix_size + 2 * border
    pixel_dim = dim * scale

    img = Image.new("RGB", (pixel_dim, pixel_dim), color="white")
    draw = ImageDraw.Draw(img)
    for r in range(matrix_size):
        for c in range(matrix_size):
            if matrix[r][c]:
                x0 = (c + border) * scale
                y0 = (r + border) * scale
                x1 = x0 + scale
                y1 = y0 + scale
                draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
