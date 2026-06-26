# Minimal pure-Python QR code encoder — MicroPython compatible
# Supports alphanumeric mode, error correction level M, versions 1-4
# No third-party imports required

# ---------------------------------------------------------------------------
# Alphanumeric character table
# ---------------------------------------------------------------------------
_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"

# ---------------------------------------------------------------------------
# QR version block layout for EC level M (from ISO 18004 / qrcode library)
# Each entry: (data_cw_per_block, ec_cw_per_block, num_blocks)
# v1-M: 1 block, 16 data, 10 EC  (total 26)
# v2-M: 1 block, 28 data, 16 EC  (total 44)
# v3-M: 1 block, 44 data, 26 EC  (total 70)
# v4-M: 2 blocks, each 32 data, 18 EC (total 100)
# ---------------------------------------------------------------------------
_BLOCK_LAYOUT = {
    1: [(16, 10, 1)],
    2: [(28, 16, 1)],
    3: [(44, 26, 1)],
    4: [(32, 18, 2)],
}

# Alphanumeric capacity for EC level M
_ALNUM_CAPACITY = {1: 20, 2: 38, 3: 61, 4: 90}

# ---------------------------------------------------------------------------
# GF(256) arithmetic — primitive polynomial 0x11d (x^8+x^4+x^3+x^2+1)
# ---------------------------------------------------------------------------
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256

def _gf_init():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11d
        x &= 0xFF
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]

_gf_init()

def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]

def _gf_pow(a, power):
    if a == 0:
        return 0
    return _GF_EXP[(_GF_LOG[a] * power) % 255]

def _gf_poly_mul(p, q):
    result = [0] * (len(p) + len(q) - 1)
    for i, pi in enumerate(p):
        for j, qj in enumerate(q):
            result[i + j] ^= _gf_mul(pi, qj)
    return result

def _gf_poly_div(dividend, divisor):
    """Polynomial division in GF(256); returns remainder."""
    result = list(dividend)
    for i in range(len(dividend) - len(divisor) + 1):
        coef = result[i]
        if coef != 0:
            for j in range(1, len(divisor)):
                if divisor[j] != 0:
                    result[i + j] ^= _gf_mul(divisor[j], coef)
    sep = len(dividend) - len(divisor) + 1
    return result[sep:]

# ---------------------------------------------------------------------------
# Reed-Solomon generator polynomials
# ---------------------------------------------------------------------------
def _rs_generator(n_ec):
    g = [1]
    for i in range(n_ec):
        g = _gf_poly_mul(g, [1, _gf_pow(2, i)])
    return g

def _rs_encode(data, n_ec):
    gen = _rs_generator(n_ec)
    # Multiply data polynomial by x^n_ec then divide by generator
    padded = list(data) + [0] * n_ec
    remainder = _gf_poly_div(padded, gen)
    return remainder

# ---------------------------------------------------------------------------
# Bit stream helper
# ---------------------------------------------------------------------------
class _Bits:
    def __init__(self):
        self.data = []  # list of 0/1 ints

    def append(self, value, length):
        for i in range(length - 1, -1, -1):
            self.data.append((value >> i) & 1)

    def to_bytes(self):
        # Pad to byte boundary
        while len(self.data) % 8 != 0:
            self.data.append(0)
        result = []
        for i in range(0, len(self.data), 8):
            byte = 0
            for bit in self.data[i:i+8]:
                byte = (byte << 1) | bit
            result.append(byte)
        return result

    def __len__(self):
        return len(self.data)

# ---------------------------------------------------------------------------
# Alphanumeric encoding
# ---------------------------------------------------------------------------
def _encode_alnum(text, version):
    bits = _Bits()
    # Mode indicator: alphanumeric = 0010
    bits.append(0b0010, 4)
    # Character count indicator length depends on version
    if version <= 9:
        cc_bits = 9
    elif version <= 26:
        cc_bits = 11
    else:
        cc_bits = 13
    bits.append(len(text), cc_bits)
    # Encode pairs
    i = 0
    while i < len(text):
        if i + 1 < len(text):
            v1 = _ALNUM.index(text[i])
            v2 = _ALNUM.index(text[i+1])
            bits.append(45 * v1 + v2, 11)
            i += 2
        else:
            v1 = _ALNUM.index(text[i])
            bits.append(v1, 6)
            i += 1
    return bits

# Pad codewords
_PAD_BYTES = [0xEC, 0x11]

def _build_data_codewords(text, version):
    bits = _encode_alnum(text, version)
    # Layout info
    layout = _BLOCK_LAYOUT[version]
    total_data_cw = sum(n * d for d, e, n in layout)
    total_bits = total_data_cw * 8

    # Terminator
    remaining = total_bits - len(bits.data)
    bits.append(0, min(4, remaining))

    # Pad to byte boundary
    while len(bits.data) % 8 != 0:
        bits.data.append(0)

    # Pad codewords
    cw_list = bits.to_bytes()
    pad_idx = 0
    while len(cw_list) < total_data_cw:
        cw_list.append(_PAD_BYTES[pad_idx % 2])
        pad_idx += 1

    return cw_list

# ---------------------------------------------------------------------------
# Interleave blocks and add EC
# ---------------------------------------------------------------------------
def _build_codewords(text, version):
    data_cw = _build_data_codewords(text, version)
    layout = _BLOCK_LAYOUT[version]

    # Split into blocks
    blocks = []
    ec_blocks = []
    idx = 0
    for data_per_block, ec_per_block, num_blocks in layout:
        for _ in range(num_blocks):
            block = data_cw[idx:idx + data_per_block]
            blocks.append(block)
            ec_blocks.append(_rs_encode(block, ec_per_block))
            idx += data_per_block

    # Interleave data codewords
    result = []
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        for b in blocks:
            if i < len(b):
                result.append(b[i])

    # Interleave EC codewords
    max_ec = max(len(b) for b in ec_blocks)
    for i in range(max_ec):
        for b in ec_blocks:
            if i < len(b):
                result.append(b[i])

    return result

# ---------------------------------------------------------------------------
# QR matrix building
# ---------------------------------------------------------------------------
def _make_matrix(size):
    return [[None] * size for _ in range(size)]

def _place_finder(mat, row, col):
    """Place a 7x7 finder pattern with top-left at (row, col)."""
    pattern = [
        [1,1,1,1,1,1,1],
        [1,0,0,0,0,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,1,1,1,0,1],
        [1,0,0,0,0,0,1],
        [1,1,1,1,1,1,1],
    ]
    for r in range(7):
        for c in range(7):
            if 0 <= row+r < len(mat) and 0 <= col+c < len(mat):
                mat[row+r][col+c] = pattern[r][c]

def _place_separator(mat, size):
    """Place separators around finder patterns."""
    # Top-left
    for i in range(8):
        if i < size: mat[7][i] = 0
        if i < size: mat[i][7] = 0
    # Top-right
    for i in range(8):
        if i < size: mat[7][size-8+i] = 0
        mat[i][size-8] = 0
    # Bottom-left
    for i in range(8):
        mat[size-8][i] = 0
        mat[size-8+i][7] = 0

def _place_timing(mat, size):
    """Place timing patterns."""
    for i in range(8, size-8):
        v = 1 if i % 2 == 0 else 0
        mat[6][i] = v
        mat[i][6] = v

def _alignment_positions(version):
    """Return alignment pattern center positions for version."""
    # From PATTERN_POSITION_TABLE in qrcode library (ISO 18004)
    _ALIGN_POS = {
        1: [],
        2: [6, 18],
        3: [6, 22],
        4: [6, 26],
    }
    pos = _ALIGN_POS.get(version, [])
    if len(pos) < 2:
        return []
    centers = []
    for r in pos:
        for c in pos:
            if not ((r == 6 and c == 6) or (r == 6 and c == pos[-1]) or (r == pos[-1] and c == 6)):
                centers.append((r, c))
    return centers

def _place_alignment(mat, version):
    centers = _alignment_positions(version)
    for (cr, cc) in centers:
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                r, c = cr+dr, cc+dc
                if abs(dr) == 2 or abs(dc) == 2:
                    mat[r][c] = 1
                elif dr == 0 and dc == 0:
                    mat[r][c] = 1
                else:
                    mat[r][c] = 0

def _reserve_format(mat, size):
    """Reserve format info areas as -1 sentinel (treated as 0 during penalty eval)."""
    for i in range(9):
        if mat[8][i] is None: mat[8][i] = -1
        if i < 8 and mat[i][8] is None: mat[i][8] = -1
    for i in range(size-8, size):
        if mat[8][i] is None: mat[8][i] = -1
        if mat[i][8] is None: mat[i][8] = -1
    # Dark module: also reserve as -1; _place_format will set it to 1

# Format info strings for EC level M (binary, 15 bits each), indexed by mask pattern 0-7
# These are precomputed: format = (EC_bits << 3 | mask) with BCH error correction and XOR mask 101010000010010
# EC level M = 00 in QR spec (bits are 00 for M in the 2-bit EC field placed as bits 14-13)
_FORMAT_INFO = [
    0b101010000010010,  # mask 0  (M, mask 0)
    0b101000100100101,  # mask 1
    0b101111001111100,  # mask 2
    0b101101101001011,  # mask 3
    0b100010111111001,  # mask 4
    0b100000011001110,  # mask 5
    0b100111110010111,  # mask 6
    0b100101010100000,  # mask 7
]

def _place_format(mat, size, mask_id):
    """Place format info using the same bit ordering as ISO 18004 (LSB-first, i=0 is bit 0).
    Vertical strip (col 8): i=0-5 → rows 0-5, i=6-7 → rows 7-8, i=8-14 → rows size-7 to size-1
    Horizontal strip (row 8): i=0-7 → cols size-1 to size-8, i=8 → col 7, i=9-14 → cols 5 to 0
    """
    fmt = _FORMAT_INFO[mask_id]

    # Vertical placement (col 8) — LSB-first
    for i in range(15):
        bit = (fmt >> i) & 1
        if i < 6:
            mat[i][8] = bit
        elif i < 8:
            mat[i + 1][8] = bit   # skip row 6 (timing)
        else:
            mat[size - 15 + i][8] = bit

    # Horizontal placement (row 8) — LSB-first
    for i in range(15):
        bit = (fmt >> i) & 1
        if i < 8:
            mat[8][size - i - 1] = bit
        elif i < 9:
            mat[8][15 - i - 1 + 1] = bit  # col 7
        else:
            mat[8][15 - i - 1] = bit

    # Dark module (always 1)
    mat[size - 8][8] = 1

def _zigzag_columns(size):
    """Generate column pairs for zigzag scan, right to left, skipping col 6 (timing)."""
    col = size - 1
    cols = []
    while col > 0:
        if col == 6:
            col -= 1
        cols.append(col)
        col -= 2
    return cols

def _place_data(mat, size, codewords):
    """Place data bits using zigzag scan."""
    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    bit_idx = 0
    cols = _zigzag_columns(size)
    for col_right in cols:
        # Upward if (size-1 - col_right) // 2 is even
        col_pair = [col_right, col_right - 1]
        upward = ((size - 1 - col_right) // 2) % 2 == 0
        row_range = range(size-1, -1, -1) if upward else range(size)
        for row in row_range:
            for c in col_pair:
                if c < 0 or c >= size:
                    continue
                if mat[row][c] is None:
                    if bit_idx < len(bits):
                        mat[row][c] = bits[bit_idx]
                        bit_idx += 1
                    else:
                        mat[row][c] = 0

# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------
def _mask_fn(pattern):
    fns = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ]
    return fns[pattern]

def _penalty(mat, size):
    score = 0
    # Rule 1: 5+ in a row of same color
    for r in range(size):
        run = 1
        for c in range(1, size):
            if mat[r][c] == mat[r][c-1]:
                run += 1
            else:
                if run >= 5: score += 3 + (run - 5)
                run = 1
        if run >= 5: score += 3 + (run - 5)
    for c in range(size):
        run = 1
        for r in range(1, size):
            if mat[r][c] == mat[r-1][c]:
                run += 1
            else:
                if run >= 5: score += 3 + (run - 5)
                run = 1
        if run >= 5: score += 3 + (run - 5)
    # Rule 2: 2x2 blocks
    for r in range(size-1):
        for c in range(size-1):
            v = mat[r][c]
            if v == mat[r][c+1] == mat[r+1][c] == mat[r+1][c+1]:
                score += 3
    # Rule 3: specific patterns
    pat1 = [1,0,1,1,1,0,1,0,0,0,0]
    pat2 = [0,0,0,0,1,0,1,1,1,0,1]
    for r in range(size):
        for c in range(size-10):
            row_seg = [mat[r][c+i] for i in range(11)]
            if row_seg == pat1 or row_seg == pat2:
                score += 40
    for c in range(size):
        for r in range(size-10):
            col_seg = [mat[r+i][c] for i in range(11)]
            if col_seg == pat1 or col_seg == pat2:
                score += 40
    # Rule 4: proportion of dark modules
    total = size * size
    dark = sum(1 for r in range(size) for c in range(size) if mat[r][c] == 1)
    pct = dark * 100 // total
    prev5 = pct - (pct % 5)
    next5 = prev5 + 5
    score += min(abs(prev5 - 50) // 5, abs(next5 - 50) // 5) * 10
    return score

# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------
def _build_qr(text, version):
    size = 4 * version + 17
    codewords = _build_codewords(text, version)

    best_mat = None
    best_score = None
    best_mask = 0

    for mask_id in range(8):
        mat = _make_matrix(size)

        # Place structural elements
        _place_finder(mat, 0, 0)
        _place_finder(mat, 0, size-7)
        _place_finder(mat, size-7, 0)
        _place_separator(mat, size)
        _place_timing(mat, size)
        _place_alignment(mat, version)
        _reserve_format(mat, size)

        # Place data
        _place_data(mat, size, codewords)

        # Apply mask to data modules only
        fn = _mask_fn(mask_id)
        # We need a function-module map
        func_mat = _make_matrix(size)
        _place_finder(func_mat, 0, 0)
        _place_finder(func_mat, 0, size-7)
        _place_finder(func_mat, size-7, 0)
        _place_separator(func_mat, size)
        _place_timing(func_mat, size)
        _place_alignment(func_mat, version)
        _reserve_format(func_mat, size)

        for r in range(size):
            for c in range(size):
                if func_mat[r][c] is None:
                    # data module
                    v = mat[r][c] if mat[r][c] is not None else 0
                    if fn(r, c):
                        mat[r][c] = v ^ 1
                    else:
                        mat[r][c] = v if v is not None else 0

        # Evaluate penalty in "test mode": format cells (-1/None) treated as 0
        # This matches the reference library which evaluates before placing real format info
        test_mat = [row[:] for row in mat]
        for r in range(size):
            for c in range(size):
                if test_mat[r][c] is None or test_mat[r][c] == -1:
                    test_mat[r][c] = 0

        score = _penalty(test_mat, size)
        if best_score is None or score < best_score:
            best_score = score
            best_mat = [row[:] for row in mat]
            best_mask = mask_id

    # Place format info for the winning mask and finalize
    _place_format(best_mat, size, best_mask)
    for r in range(size):
        for c in range(size):
            if best_mat[r][c] is None or best_mat[r][c] == -1:
                best_mat[r][c] = 0

    return best_mat, size

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
QUIET_ZONE = 4

def make(content):
    """Return a list of strings of '0'/'1', including 4-module quiet zone."""
    # Validate characters
    for ch in content:
        if ch not in _ALNUM:
            raise ValueError("Character not in alphanumeric set: " + ch)

    # Select version
    version = None
    for v, capacity in _ALNUM_CAPACITY.items():
        if len(content) <= capacity:
            version = v
            break
    if version is None:
        raise ValueError("Content too long for version 1-4")

    mat, size = _build_qr(content, version)

    # Add quiet zone
    qz = QUIET_ZONE
    full_size = size + 2 * qz
    rows = []
    quiet_row = "0" * full_size
    for _ in range(qz):
        rows.append(quiet_row)
    for r in range(size):
        row = "0" * qz + "".join(str(mat[r][c]) for c in range(size)) + "0" * qz
        rows.append(row)
    for _ in range(qz):
        rows.append(quiet_row)

    return rows
