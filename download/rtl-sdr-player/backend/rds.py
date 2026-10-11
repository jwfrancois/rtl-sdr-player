"""RDS (Radio Data System) subcarrier decoder + test-signal encoder.

The decoder recovers the 57 kHz suppressed-carrier BPSK subcarrier from the
FM discriminator output of the wideband-FM chain:

    discriminator out (240 kSPS real)
      -> mix to DC at 57 kHz (ppm-corrected from the 19 kHz pilot)
      -> decimate /5 -> 48 kSPS
      -> decimate /5 (2.6 kHz LPF) -> 9.6 kSPS complex
      -> Costas loop (carrier recovery, decision-directed BPSK)
      -> Gardner timing recovery (1187.5 bps)
      -> differential decode -> block sync via CRC-10 syndrome
      -> group parse (PS name, RadioText, PTY, AF, clock time)

Protocol constants follow IEC 62106 section B.3.1 (parity check matrix,
offset words and syndromes) and were cross-verified against the redsea
decoder and by numerical encode/decode round trips (scripts/verify_rds.py).

USA note: program type names use the RBDS (North American) table, and the
companion de-emphasis constant is pinned to 75 microseconds in config.py.
"""
import math
import time
from collections import deque

import numpy as np
from scipy import signal as sps

BITS_PER_SECOND = 1187.5
SUBCARRIER_HZ = 57_000.0

# --------------------------------------------------------------------------
# IEC 62106 B.3.1 constants
# --------------------------------------------------------------------------
# Parity check matrix rows (26 x 10). Bit p of a 26-bit word (p = 25-k for
# row k) contributes row k to the 10-bit syndrome.
_H = (
    0b1000000000, 0b0100000000, 0b0010000000, 0b0001000000,
    0b0000100000, 0b0000010000, 0b0000001000, 0b0000000100,
    0b0000000010, 0b0000000001,
    0b1011011100, 0b0101101110, 0b0010110111, 0b1010000111,
    0b1110011111, 0b1100010011, 0b1101010101, 0b1101110110,
    0b0110111011, 0b1000000001, 0b1111011100, 0b0111101110,
    0b0011110111, 0b1010100111, 0b1110001111, 0b1100011011,
)

# Transmitted offset words (modulo-2 added to the CRC by the encoder)
OFFSET_WORDS = {"A": 0b0011111100, "B": 0b0110011000, "C": 0b0101101000,
                "Cp": 0b1101010000, "D": 0b0110110100}

# Syndrome of a valid block, per offset
BLOCK_SYNDROME = {"A": 0b1111011000, "B": 0b1111010100, "C": 0b1001011100,
                  "Cp": 0b1111001100, "D": 0b1001011000}
SYNDROME_TO_BLOCK = {v: k for k, v in BLOCK_SYNDROME.items()}


def _syndrome(word26):
    """10-bit syndrome of a 26-bit RDS block (0 -> valid for its offset)."""
    s = 0
    for k in range(26):
        if (word26 >> (25 - k)) & 1:
            s ^= _H[k]
    return s


# Check-bit rows: checkword bit b (LSB at word position b) contributes
# row _H[25-b]. Precompute the (invertible) map  checkword -> syndrome  and
# invert it once, so the encoder can solve for check bits directly.
_MCR = [_H[25 - b] for b in range(10)]


def _combine(c):
    s = 0
    for b in range(10):
        if (c >> b) & 1:
            s ^= _MCR[b]
    return s


_CHECK_TO_SYN = {_combine(c): c for c in range(1024)}
if len(_CHECK_TO_SYN) != 1024:
    raise RuntimeError("RDS check matrix is singular - constants are wrong")
# _CHECK_TO_SYN maps syndrome-contribution -> checkword, which is exactly the
# inverse the encoder needs (combine(check) == required syndrome).


class BlockCoder:
    """Encodes/verifies RDS blocks (16 info bits + 10 check bits)."""

    @staticmethod
    def word(info16, block):
        """26-bit transmitted word for an information word + block letter."""
        if block not in BLOCK_SYNDROME:
            raise ValueError(f"bad block letter {block!r}")
        s0 = _syndrome((info16 & 0xFFFF) << 10)
        check = _CHECK_TO_SYN[BLOCK_SYNDROME[block] ^ s0]
        return ((info16 & 0xFFFF) << 10) | check

    @staticmethod
    def verify(word26):
        """Return the block letter if the 26-bit word is a valid block."""
        return SYNDROME_TO_BLOCK.get(_syndrome(word26))


# RBDS (North America) programme type names - USA uses this table
PTY_NAMES_RBDS = [
    "None", "News", "Information", "Sports", "Talk", "Rock",
    "Classic Rock", "Adult Hits", "Soft Rock", "Top 40", "Country",
    "Oldies", "Soft", "Nostalgia", "Jazz", "Classical",
    "Rhythm & Blues", "Soft Rhythm & Blues", "Foreign Language",
    "Religious Music", "Religious Talk", "Personality", "Public",
    "College", "Spanish Talk", "Spanish Music", "Hip Hop",
    "Unassigned", "Unassigned", "Weather", "Emergency Test", "Emergency",
]


def _sanitize_char(code):
    code &= 0xFF
    if 0x20 <= code <= 0x7E:
        return chr(code)
    if code == 0x0D:
        return "\r"
    return " "


def _af_code_to_mhz(code):
    """RDS alternative-frequency code -> MHz (VHF codes 1-204), else None."""
    if 1 <= code <= 204:
        return round(87.5 + 0.1 * code, 1)
    return None


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------
class RdsDecoder:
    """Recovers and decodes RDS from the WFM discriminator stream."""

    IF_IN = 240_000        # discriminator output rate fed to process()
    MID_RATE = 48_000
    LOW_RATE = 9_600       # Costas / timing input rate

    # loop gains (empirical, tuned for clean-to-moderate signals)
    _COSTAS_KP = 0.12
    _COSTAS_KI = 0.006
    _TIMING_KP = 0.10
    _TIMING_KI = 0.0012

    TAIL = 20  # overlap samples carried between blocks for the timing loop

    def __init__(self, if_rate=IF_IN):
        self.fs = float(if_rate)
        self._nco_inc = 2.0 * np.pi * SUBCARRIER_HZ / self.fs
        self._nco_phase = 0.0
        self._ppm = 0.0
        ratio = if_rate / float(self.IF_IN)
        self._dec1 = _Dec(5, 18_000, self.MID_RATE * ratio, numtaps=191)
        self._dec2 = _Dec(5, 2_600, self.MID_RATE * ratio, numtaps=255)
        # Costas state
        self._phi = 0.0
        self._freq = 0.0
        # Gardner timing state (positions in samples at LOW_RATE)
        self._sps_base = self.LOW_RATE * ratio / (BITS_PER_SECOND * ratio)
        self._sps_nom = self._sps_base
        self._sps = self._sps_nom
        self._pos = self._sps_nom
        self._prev_late = 0.0
        self._amp = 0.02
        self._tail = [0.0] * self.TAIL
        # bit pipeline
        self._bits = deque(maxlen=64)
        self._diff_prev = 0
        self._sync = "search"     # search | locked
        self._expect = "A"        # expected block letter when locked
        self._cur = []            # bits of the block being collected
        self._blocks = []         # completed blocks (info words) of the group
        # parsed data
        self._pi = None
        self._ps = [" "] * 8
        self._ps_missing = set(range(8))
        self._ps_ready = None
        self._rt = None
        self._rt_len = 64
        self._rt_ab = 0
        self._af = set()
        self._pty = None
        self._tp = None
        self._ta = None
        self._ms = None
        self._clock = None
        self._groups = 0
        self._errors = 0
        self.updated_at = 0.0

    # -- external ppm correction (from the 19 kHz pilot tracker) -----------
    def set_ppm(self, ppm):
        if abs(ppm - self._ppm) > 0.5:
            self._ppm = ppm
            f = SUBCARRIER_HZ * (1.0 + ppm * 1e-6)
            self._nco_inc = 2.0 * np.pi * f / self.fs
            self._sps_nom = self._sps_base / (1.0 + ppm * 1e-6)

    # -- main entry: one block of discriminator output (real floats) -------
    def process(self, discrim):
        x = np.asarray(discrim, dtype=np.float64)
        n = len(x)
        ph = self._nco_phase + self._nco_inc * np.arange(n)
        mix = np.exp(-1j * ph)
        self._nco_phase = float((self._nco_phase + self._nco_inc * n) % (2.0 * np.pi))
        z = (x * mix).astype(np.complex64)
        z = self._dec2.process(self._dec1.process(z))
        if len(z) < 16:
            return
        self._carrier_and_bits(z)

    def _carrier_and_bits(self, z):
        zl = z.tolist()
        n = len(zl)
        phi = self._phi
        freq = self._freq
        kp, ki = self._COSTAS_KP, self._COSTAS_KI
        i_vals = [0.0] * n
        amp = self._amp
        for i in range(n):
            s = zl[i]
            c = math.cos(phi)
            sn = math.sin(phi)
            vr = s.real * c + s.imag * sn
            vi = -s.real * sn + s.imag * c
            i_vals[i] = vr
            amp = 0.999 * amp + 0.001 * abs(vr)
            err = (vi / (amp if amp > 1e-4 else 1e-4)) \
                * (1.0 if vr >= 0.0 else -1.0)
            freq += ki * err
            if freq > 0.35:
                freq = 0.35
            elif freq < -0.35:
                freq = -0.35
            phi += freq + kp * err
            if phi > 2.0 * np.pi:
                phi -= 2.0 * np.pi
            elif phi < 0.0:
                phi += 2.0 * np.pi
        self._phi = phi
        self._freq = freq
        self._amp = amp

        # ---- Gardner timing on the recovered BPSK waveform ---------------
        # Overlap the tail of the previous block so strobes and decisions
        # continue seamlessly across block boundaries.
        ext = self._tail + i_vals
        self._tail = i_vals[-self.TAIL:]
        off = self.TAIL
        sps = self._sps
        pos = self._pos + off
        prev_late = self._prev_late
        kp2, ki2 = self._TIMING_KP, self._TIMING_KI
        a2 = max(amp * amp, 1e-6)
        lim_hi = self._sps_nom * 1.01
        lim_lo = self._sps_nom * 0.99
        half = sps / 2.0
        out = []
        end = len(ext) - 1
        while pos + half < end:
            mid = _interp(ext, pos)
            late = _interp(ext, pos + half)
            # Gardner TED: (late - early) * mid; positive error means the
            # strobe ran late, which must slow the timing advance down.
            err = (late - prev_late) * mid / a2
            if err > 1.0:
                err = 1.0
            elif err < -1.0:
                err = -1.0
            sps += ki2 * err
            if sps > lim_hi:
                sps = lim_hi
            elif sps < lim_lo:
                sps = lim_lo
            pos += sps + kp2 * err
            out.append(mid)
            prev_late = late
        self._sps = sps
        self._pos = pos - off - n
        self._prev_late = prev_late

        for mid in out:
            self._push_bit(1 if mid >= 0.0 else 0)

    # -- bit-level state machine -------------------------------------------
    def _push_bit(self, bit):
        d = bit ^ self._diff_prev
        self._diff_prev = bit
        if self._sync == "search":
            self._bits.append(d)
            if len(self._bits) < 26:
                return
            w = 0
            for b in tuple(self._bits)[-26:]:   # last 26 bits only
                w = (w << 1) | b
            letter = BlockCoder.verify(w)
            if letter == "A":  # lock on a group-start block for clean assembly
                self._sync = "locked"
                self._expect = _next_block(letter)
                self._cur = []
                self._blocks = [w >> 10]
                self._bits.clear()
        else:
            self._cur.append(d)
            if len(self._cur) < 26:
                return
            w = 0
            for b in self._cur:
                w = (w << 1) | b
            letter = BlockCoder.verify(w)
            if letter != self._expect and not (
                    self._expect in ("C", "Cp") and letter in ("C", "Cp")):
                self._errors += 1
                self._resync()
                return
            self._cur = []
            self._blocks.append(w >> 10)
            self._expect = _next_block(letter)
            if letter == "D":
                self._parse_group(self._blocks)
                self._blocks = []

    def _resync(self):
        self._sync = "search"
        self._cur = []
        self._blocks = []
        self._bits.clear()

    # -- group parsing -------------------------------------------------------
    def _parse_group(self, blocks):
        b_a, b_b = blocks[0], blocks[1]
        b_c = blocks[2] if len(blocks) > 2 else None
        b_d = blocks[3] if len(blocks) > 3 else None
        self._pi = b_a
        gtype = (b_b >> 12) & 0xF
        version = (b_b >> 11) & 1
        self._tp = (b_b >> 10) & 1
        self._pty = (b_b >> 5) & 0x1F
        self._groups += 1
        self.updated_at = time.time()

        if gtype == 0:
            seg = b_b & 0x3
            self._ta = (b_b >> 4) & 1
            self._ms = (b_b >> 3) & 1
            if version == 0 and b_c is not None:
                for code in ((b_c >> 8) & 0xFF, b_c & 0xFF):
                    mhz = _af_code_to_mhz(code)
                    if mhz is not None:
                        self._af.add(mhz)
                        if len(self._af) > 12:
                            self._af.pop()
            if b_d is not None:
                self._ps[seg * 2] = _sanitize_char((b_d >> 8) & 0xFF)
                self._ps[seg * 2 + 1] = _sanitize_char(b_d & 0xFF)
                self._ps_missing.discard(seg * 2)
                self._ps_missing.discard(seg * 2 + 1)
                if not self._ps_missing:
                    self._ps_ready = "".join(self._ps)
        elif gtype == 2:
            self._decode_radiotext(b_b, b_c, b_d, version)
        elif gtype == 4 and version == 0 and b_c is not None and b_d is not None:
            self._decode_clock(b_b, b_c, b_d)

    def _decode_radiotext(self, b_b, b_c, b_d, version):
        seg = b_b & 0xF
        ab = (b_b >> 4) & 1
        if ab != self._rt_ab or self._rt is None:
            self._rt_ab = ab
            self._rt_len = 64 if version == 0 else 32
            self._rt = [" "] * self._rt_len
        chars = []
        if version == 0 and b_c is not None:
            chars = [(b_c >> 8) & 0xFF, b_c & 0xFF,
                     (b_d or 0) >> 8 & 0xFF, (b_d or 0) & 0xFF]
            base = seg * 4
        else:
            if b_d is None:
                return
            chars = [(b_d >> 8) & 0xFF, b_d & 0xFF]
            base = seg * 2
        for k, code in enumerate(chars):
            if base + k < self._rt_len:
                self._rt[base + k] = _sanitize_char(code)

    def _decode_clock(self, b_b, b_c, b_d):
        mjd = ((b_b & 0x3) << 15) | ((b_c >> 1) & 0x7FFF)
        hour = (((b_c & 1) << 4) | ((b_d >> 12) & 0xF)) & 0x1F
        minute = (b_d >> 6) & 0x3F
        sense = (b_d >> 5) & 1
        off_half = b_d & 0x1F
        if hour > 23 or minute > 59 or off_half > 23:
            return
        try:
            import datetime as _dt
            day = _dt.date(1970, 1, 1) + _dt.timedelta(days=mjd - 40587)
            off = -off_half * 0.5 if sense else off_half * 0.5
            loc = _dt.datetime(day.year, day.month, day.day, hour, minute) \
                + _dt.timedelta(hours=off)
            sign = "+" if off >= 0 else "-"
            oh = int(abs(off))
            om = int(round((abs(off) - oh) * 60))
            self._clock = (f"{loc.year:04d}-{loc.month:02d}-{loc.day:02d} "
                           f"{loc.hour:02d}:{loc.minute:02d} UTC{sign}{oh:02d}:{om:02d}")
        except (ValueError, OverflowError):
            pass

    # -- output --------------------------------------------------------------
    def snapshot(self):
        ps = self._ps_ready.rstrip() if self._ps_ready else None
        rt = None
        if self._rt is not None:
            rt = "".join(self._rt).rstrip()
            rt = rt.split("\r")[0].strip() or None
        return {
            "pi": f"{self._pi:04X}" if self._pi is not None else None,
            "ps": ps,
            "pty": PTY_NAMES_RBDS[self._pty] if self._pty is not None else None,
            "tp": self._tp,
            "ta": self._ta,
            "ms": self._ms,
            "radiotext": rt,
            "clock": self._clock,
            "af_mhz": sorted(self._af)[:12],
            "groups": self._groups,
            "errors": self._errors,
            "locked": self._sync == "locked",
        }


def _next_block(letter):
    return {"A": "B", "B": "C", "C": "D", "Cp": "D", "D": "A"}[letter]


def _interp(buf, pos):
    i = int(pos)
    frac = pos - i
    if i < 0:
        i = 0
        frac = 0.0
    a = buf[i]
    return a + frac * (buf[i + 1] - a)


class _Dec:
    """Minimal FIR decimator with state (complex input)."""

    def __init__(self, factor, cutoff_hz, fs, numtaps):
        self.factor = int(factor)
        self.taps = sps.firwin(numtaps, cutoff_hz / (fs / 2.0)).astype(np.float32)
        self.zi = np.zeros(len(self.taps) - 1, dtype=np.complex64)

    def process(self, x):
        y, self.zi = sps.lfilter(self.taps, 1.0, x, zi=self.zi)
        return y[:: self.factor].astype(np.complex64)

    def reset(self):
        self.zi[:] = 0


# --------------------------------------------------------------------------
# Test-signal encoder (used by the simulator so RDS can be demoed & tested
# without a real station)
# --------------------------------------------------------------------------
class RdsEncoder:
    """Builds a repeating RDS bit stream (0A + 2A groups) for the simulator."""

    def __init__(self, pi, ps, radiotext, pty=9, tp=0, ms=1, ta=0,
                 af_codes=()):
        """af_codes are raw RDS AF codes (1-204 -> 87.5 + 0.1*code MHz)."""
        self.pi = int(pi) & 0xFFFF
        ps = (ps or "").upper()[:8].ljust(8)
        self.ps = ps
        rt = (radiotext or "")[:64]
        self.rt = rt
        blocks = []
        af_bytes = [c & 0xFF for c in list(af_codes)[:2]] + [0] * 2
        common = (tp & 1) << 10 | (pty & 0x1F) << 5
        # 4 x group 0A: PS segments 0..3, block C carries the AF codes
        for seg in range(4):
            b_b = common | (ta & 1) << 4 | (ms & 1) << 3 | seg
            b_c = (af_bytes[0] << 8) | af_bytes[1]
            chars = ps[seg * 2: seg * 2 + 2].encode("ascii", "replace")
            b_d = (chars[0] << 8) | chars[1]
            blocks += [BlockCoder.word(self.pi, "A"),
                       BlockCoder.word(b_b, "B"),
                       BlockCoder.word(b_c, "C"),
                       BlockCoder.word(b_d, "D")]
        # RadioText via 2A groups (4 chars per group, A/B flag held at 0)
        padded = rt + " " * ((-len(rt)) % 4)
        for seg in range(len(padded) // 4):
            b_b = (0x2 << 12) | common | (seg & 0xF)
            chunk = padded[seg * 4: seg * 4 + 4].encode("ascii", "replace")
            b_c = (chunk[0] << 8) | chunk[1]
            b_d = (chunk[2] << 8) | chunk[3]
            blocks += [BlockCoder.word(self.pi, "A"),
                       BlockCoder.word(b_b, "B"),
                       BlockCoder.word(b_c, "C"),
                       BlockCoder.word(b_d, "D")]
        # flatten one cycle to a bit stream (MSB first), then repeat it many
        # times BEFORE differential encoding. Differential coding is only
        # consistent across a repetition boundary if the data has even
        # parity, so a finite period would corrupt one bit per cycle; a long
        # linear stream reduces that glitch to once per ~reps*1.05 s.
        base = []
        for w in blocks:
            for k in range(25, -1, -1):
                base.append((w >> k) & 1)
        reps = 64
        data = base * reps
        enc = []
        prev = 0
        for b in data:
            prev ^= b
            enc.append(prev)
        self.bits = np.array(enc, dtype=np.uint8)
        # Nyquist pulse shaping (raised cosine, alpha=1, 16x oversampled,
        # circular convolution => periodic stream with zero ISI at strobes)
        ov = 16
        rate = BITS_PER_SECOND * ov
        span = 8 * ov
        x = (np.arange(-span, span + 1) / ov)  # in symbol periods
        alpha = 1.0
        num = np.sinc(x) * np.cos(np.pi * alpha * x)
        den = 1.0 - (2.0 * alpha * x) ** 2
        h = np.where(np.abs(den) < 1e-9,
                     (np.pi / 4.0) * np.sinc(1.0 / (2.0 * alpha)),
                     num / np.where(np.abs(den) < 1e-9, 1.0, den))
        h = h / h.sum()
        up = np.repeat(np.where(self.bits == 1, 1.0, -1.0), ov)
        n = len(up)
        h_pad = np.zeros(n)
        h_pad[: len(h)] = h
        h_pad = np.roll(h_pad, -(len(h) // 2))  # zero-delay, circular
        self._shape = np.fft.irfft(
            np.fft.rfft(up) * np.fft.rfft(h_pad), n).astype(np.float32)
        self._shape_rate = rate

    def shaped(self, t):
        """Shaped NRZ data waveform s(t) in [-1.2, 1.2] at continuous time t."""
        pos = t * self._shape_rate
        base = np.floor(pos)
        i = base.astype(np.int64) % len(self._shape)
        j = (i + 1) % len(self._shape)
        frac = pos - base
        return self._shape[i] + frac * (self._shape[j] - self._shape[i])

    def subcarrier_dev(self, t, ppm=0.0):
        """RDS contribution to the FM deviation (Hz): DSB-SC data on the
        57 kHz subcarrier, +/-2 kHz peak (+/-5.2% injection scaled)."""
        f = 57000.0 * (1.0 + ppm * 1e-6)
        return 2000.0 * self.shaped(t) * np.cos(2.0 * np.pi * f * t)
