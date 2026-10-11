"""Numerical verification of RDS block coding before building rds.py.

Checks, per IEC 62106 B.3.1 (constants transcribed from the redsea decoder):
  1. syndrome(offset word alone) == published syndrome constant? (sanity)
  2. Encoder: solve check bits from syndrome target -> decoder round trip.
  3. Differential encode -> decode round trip.
"""
import numpy as np

H = [0b1000000000, 0b0100000000, 0b0010000000, 0b0001000000,
     0b0000100000, 0b0000010000, 0b0000001000, 0b0000000100,
     0b0000000010, 0b0000000001,
     0b1011011100, 0b0101101110, 0b0010110111, 0b1010000111,
     0b1110011111, 0b1100010011, 0b1101010101, 0b1101110110,
     0b0110111011, 0b1000000001, 0b1111011100, 0b0111101110,
     0b0011110111, 0b1010100111, 0b1110001111, 0b1100011011]

SYNDROME = {"A": 0b1111011000, "B": 0b1111010100, "C": 0b1001011100,
            "Cp": 0b1111001100, "D": 0b1001011000}
OFFSET = {"A": 0b0011111100, "B": 0b0110011000, "C": 0b0101101000,
          "Cp": 0b1101010000, "D": 0b0110110100}


def syndrome(word26: int) -> int:
    s = 0
    for k in range(26):
        if (word26 >> (25 - k)) & 1:
            s ^= H[k]
    return s


# Check bit b (LSB of the 10-bit checkword sits at word position b) contributes
# row H[25-b] to the syndrome. Build the (invertible) map c -> Mcr.c by brute
# force over all 1024 candidates and invert it: bulletproof for a one-time
# precomputation.
Mcr = [H[25 - b] for b in range(10)]


def _combine(c: int) -> int:
    s = 0
    for b in range(10):
        if (c >> b) & 1:
            s ^= Mcr[b]
    return s


_fwd = {_combine(c): c for c in range(1024)}
assert len(_fwd) == 1024, "check matrix singular"
sols = [0] * 1024  # sols[i] = check bits when need bit i is the... (per-bit use)
for need_val, c_val in _fwd.items():
    sols[need_val] = c_val


def encode_check(info16: int, block: str) -> int:
    s0 = syndrome((info16 << 10) & 0x3FFFFFF)
    need = SYNDROME[block] ^ s0
    return sols[need]


rng = np.random.default_rng(7)
fails = 0
# 1. offset-word sanity (information word = 0)
for blk in SYNDROME:
    s = syndrome(OFFSET[blk])
    ok = s == SYNDROME[blk]
    fails += (not ok)
    print(f"block {blk}: syndrome(0||offset) = {s:010b} expected {SYNDROME[blk]:010b} {'OK' if ok else 'FAIL'}")

# 2. random round trip (encode_check yields the *transmitted* checkword,
#    i.e. crc ^ offset already folded in)
for trial in range(500):
    blk = list(SYNDROME)[trial % 5]
    info = int(rng.integers(0, 1 << 16))
    chk = encode_check(info, blk)
    w = (info << 10) | chk
    if syndrome(w) != SYNDROME[blk]:
        fails += 1
        print("ROUNDTRIP FAIL", blk, hex(info), f"{chk:010b}")
        break
else:
    print("500 random encode/decode round trips: OK")

# 3. differential round trip
for trial in range(50):
    bits = rng.integers(0, 2, 104)
    enc = np.empty(104, dtype=np.uint8)
    prev = 0
    for i, b in enumerate(bits):
        prev ^= int(b)
        enc[i] = prev
    prev = 0
    dec = np.empty(104, dtype=np.uint8)
    for i, b in enumerate(enc):
        dec[i] = int(b) ^ prev
        prev = int(b)
    if not (dec == bits).all():
        fails += 1
        print("DIFF FAIL")
        break
else:
    print("differential encode/decode round trips: OK")

print("ALL OK" if fails == 0 else f"{fails} FAILURES")
