"""DSP engine: frequency shifting, decimation, FM/AM demodulation, PSD.

All processing is vectorized numpy/scipy operating on block streams, sized to
run comfortably faster than real time on a desktop CPU.

Signal path (FM broadcast):
    IQ @ 2.4 MSPS -> NCO shift (station to DC) -> decimate x10 -> 240 kSPS
    -> polar discriminator -> audio LPF + decimate x5 -> 48 kHz
    -> de-emphasis IIR -> int16 mono
    (a tap of the discriminator output also feeds the 19 kHz pilot tracker
     and the 57 kHz RDS decoder - see backend/rds.py)

Signal path (NOAA weather / narrowband FM):
    IQ @ 2.4 MSPS -> NCO shift (offset tuned) -> decimate x10 -> 240 kSPS
    -> polar discriminator (5 kHz deviation) -> 8 kHz channel LPF
    -> 3.4 kHz audio LPF + 300 Hz high-pass + AGC -> int16 mono

Signal path (AM broadcast, direct sampling):
    IQ @ 2.4 MSPS -> NCO shift -> decimate x10 -> 240 kSPS -> decimate x5
    (sharp 7.5 kHz channel filter) -> 48 kSPS -> envelope detect
    -> DC blocker + AGC -> int16 mono

Signal path (US airband 118-137 MHz, AM):
    IQ @ 2.4 MSPS -> offset-tuned like FM -> envelope detect after mixing
    the station back to DC -> DC blocker + AGC -> int16 mono
"""
import numpy as np
from scipy import signal

from . import rds as rds_mod

AUDIO_RATE = 48_000
# capture block ~109 ms; must stay divisible by the 10x5 decimation chain so
# the decimated sample grid stays uniform (keeps the pilot ppm estimator honest)
BLOCK = 262_140

# FM deviations used to normalize demodulated audio to +/-1.0 full scale
WFM_DEV_HZ = 75_000
NBFM_DEV_HZ = 5_000
PILOT_HZ = 19_000


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------
class NcoMixer:
    """Complex frequency shifter with continuous phase across blocks."""

    def __init__(self, freq_hz, fs):
        self.fs = float(fs)
        self.set_freq(freq_hz)

    def set_freq(self, freq_hz):
        self.freq = float(freq_hz)
        self.phase = 0.0
        self._inc = 2.0 * np.pi * self.freq / self.fs

    def process(self, x):
        n = len(x)
        ph = self.phase + self._inc * np.arange(n, dtype=np.float64)
        mix = np.exp(-1j * ph)
        self.phase = float((self.phase + self._inc * n) % (2.0 * np.pi))
        return (x * mix.astype(np.complex64)).astype(np.complex64)


def lowpass_taps(numtaps, cutoff_hz, fs):
    return signal.firwin(numtaps, cutoff_hz / (fs / 2.0)).astype(np.float32)


class Decimator:
    """FIR decimation stage: anti-alias filter + downsample."""

    def __init__(self, factor, cutoff_hz, fs, numtaps=129):
        self.factor = int(factor)
        self.taps = lowpass_taps(numtaps, cutoff_hz, fs)
        self.zi = np.zeros(len(self.taps) - 1, dtype=np.complex64)

    def process(self, x):
        y, self.zi = signal.lfilter(
            self.taps, 1.0, x, zi=self.zi)
        return y[:: self.factor].astype(np.complex64)

    def reset(self):
        self.zi[:] = 0


class DeEmphasis:
    """Single-pole FM de-emphasis.

    The time constant is pinned to 75 microseconds for the USA (Americas and
    Korea broadcast with 75 us; most of Europe/Asia uses 50 us). It stays
    configurable in config.json for listeners outside the US.
    """

    def __init__(self, tau_us, fs=AUDIO_RATE):
        alpha = np.exp(-1.0 / (fs * tau_us * 1e-6))
        self.b = np.array([1.0 - alpha], dtype=np.float64)
        self.a = np.array([1.0, -(alpha)], dtype=np.float64)
        self.zi = np.zeros(1)

    def process(self, x):
        y, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return y

    def reset(self):
        self.zi[:] = 0


class PilotWatch:
    """Tracks the 19 kHz stereo pilot tone in the discriminator output.

    Gives the UI a stereo/pilot indicator and, more importantly, measures the
    tuner's frequency error: the pilot's offset from exactly 19 kHz equals
    the ppm error of the dongle clock, which the RDS decoder uses to place
    its 57 kHz mixing NCO on the true subcarrier frequency.
    """

    def __init__(self, fs=240_000.0):
        self.fs = float(fs)
        self._n0 = 0
        self._prev_phase = None
        self._freq_off = 0.0
        self.present = False
        self.rel_db = -99.0

    def process(self, d):
        """d: real discriminator output (rad/sample) for one block."""
        d = np.asarray(d, dtype=np.float64)
        n = len(d)
        w = np.exp(-2j * np.pi * PILOT_HZ
                   * (self._n0 + np.arange(n)) / self.fs)
        self._n0 += n
        g = np.dot(d, w)
        power = (abs(g) ** 2) * 4.0 / (n * n)
        sig = float(np.mean(d ** 2)) + 1e-12
        self.rel_db = 10.0 * np.log10(power / sig)
        self.present = self.rel_db > -25.0
        ph = float(np.angle(g))
        if self._prev_phase is not None and self.present:
            dd = (ph - self._prev_phase + np.pi) % (2.0 * np.pi) - np.pi
            df = dd / (2.0 * np.pi * n / self.fs)
            if abs(df) < 50.0:
                self._freq_off = 0.8 * self._freq_off + 0.2 * df
        self._prev_phase = ph

    @property
    def ppm(self):
        return self._freq_off / PILOT_HZ * 1e6


# --------------------------------------------------------------------------
# Demodulators
# --------------------------------------------------------------------------
class WfmDemod:
    """Wideband FM broadcast demodulator (mono output + RDS tap)."""

    IF_RATE = 240_000

    def __init__(self, fs, lo_offset_hz, de_emphasis_us):
        self.fs = float(fs)
        self.lo_offset = float(lo_offset_hz)
        # NcoMixer(f) shifts the spectrum DOWN by f Hz: the device tunes
        # below the station, so +offset brings it to exact DC
        self.nco = NcoMixer(self.lo_offset, self.fs)
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)   # -> 240 kSPS
        self.dec2 = Decimator(5, 15_500, self.IF_RATE, numtaps=129)  # -> 48 kHz
        self.deemph = DeEmphasis(de_emphasis_us)
        self.pilot = PilotWatch(self.IF_RATE)
        self.rds = rds_mod.RdsDecoder(self.IF_RATE)
        self.last_chan_db = None
        self._last = np.complex64(0)

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        # channel-power metric: pre-detection RF power in the +/-120 kHz
        # decimated window (v1.4). Demodulated AUDIO power depends on the
        # station's deviation and even the current programme loudness, and
        # off-channel filter skirts can read hotter than a quiet station —
        # RF channel power is what "is someone broadcasting here" means,
        # and it matches the NBFM/AM squelch metrics.
        self.last_chan_db = 10.0 * float(np.log10(
            np.mean(np.abs(x) ** 2) + 1e-12))
        # polar discriminator with 1-sample memory across blocks
        z = np.concatenate(([self._last], x))
        self._last = x[-1]
        d = np.angle(z[1:] * np.conj(z[:-1]))
        # pilot tracking + RDS decoding run on the discriminator tap
        self.pilot.process(d)
        if self.pilot.present:
            self.rds.set_ppm(self.pilot.ppm)
        self.rds.process(d)
        audio = d * (self.IF_RATE / (2.0 * np.pi * WFM_DEV_HZ))
        y = self.dec2.process(audio.astype(np.complex64))
        audio = self.deemph.process(y.real.astype(np.float64))
        return to_int16(audio)

    def reset(self):
        self.nco.set_freq(self.lo_offset)
        self.dec1.reset()
        self.dec2.reset()
        self.deemph.reset()
        self.pilot = PilotWatch(self.IF_RATE)
        self.rds = rds_mod.RdsDecoder(self.IF_RATE)
        self._last = np.complex64(0)


class CtcssDetector:
    """Goertzel CTCSS tone decoder for the NBFM audio path.

    Tapped AFTER the 3.4 kHz voice low-pass but BEFORE the 300 Hz high-pass:
    the sub-audible tone (67-250.3 Hz) is modulated onto the carrier like any
    other audio, so it survives FM demodulation and the voice LPF, but the
    300 Hz high-pass that cleans up the voice band would erase it.

    Three-part decision, evaluated on a 0.25 s sliding window:
      * deviation estimate  2|X(f_t)|/N scaled to Hz (opens above ~150 Hz,
        typical transmitter tone deviation is 300-500 Hz)
      * energy concentration - the tone must dominate its +/-7 Hz neighbours
        (rejects wobbly voice fundamentals that stray into the tone band)
      * persistence - two consecutive windows before opening (~0.5 s),
        one window below the close threshold to shut again
    """

    OPEN_DEV_HZ = 150.0
    CLOSE_DEV_HZ = 100.0
    MIN_TONE_RATIO = 1.8     # centre bin vs +/-7 Hz neighbour bins
    NEIGHBOUR_HZ = 7.0

    def __init__(self, fs=AUDIO_RATE, window_s=0.25):
        self.fs = float(fs)
        self.win_n = int(round(fs * window_s))
        self.tone_hz = 0.0
        self.dev_hz = 0.0        # estimated tone deviation (Hz)
        self.ratio = 0.0         # centre vs neighbour bin magnitude
        self.open = False
        self._buf = np.zeros(0, dtype=np.float64)
        self._kern = self._kern_lo = self._kern_hi = None
        self._open_cnt = 0

    def set_tone(self, hz):
        """Select the tone to decode (Hz); 0 disables the detector."""
        hz = float(hz or 0.0)
        if hz == self.tone_hz:
            return
        self.tone_hz = hz
        self._buf = np.zeros(0, dtype=np.float64)
        self.open = False
        self.dev_hz = 0.0
        self.ratio = 0.0
        self._open_cnt = 0
        if hz > 0.0:
            n = np.arange(self.win_n, dtype=np.float64)
            base = -2j * np.pi * hz / self.fs
            d = 2j * np.pi * self.NEIGHBOUR_HZ / self.fs
            self._kern = np.exp(base * n)
            self._kern_lo = np.exp((base - d) * n)
            self._kern_hi = np.exp((base + d) * n)
        else:
            self._kern = self._kern_lo = self._kern_hi = None

    def process(self, audio):
        """Feed one block of pre-high-pass NBFM audio (48 kHz float)."""
        if self._kern is None:
            self.open = False
            return
        self._buf = np.concatenate(
            (self._buf, np.asarray(audio, dtype=np.float64)))
        if len(self._buf) < self.win_n:
            return
        if len(self._buf) > 2 * self.win_n:
            self._buf = self._buf[-self.win_n:]
        seg = self._buf[-self.win_n:]
        mag = abs(np.dot(seg, self._kern))
        nbr = (abs(np.dot(seg, self._kern_lo))
               + abs(np.dot(seg, self._kern_hi)))
        self.dev_hz = 2.0 * mag / self.win_n * NBFM_DEV_HZ
        self.ratio = mag / (nbr + 1e-9)
        stable = self.ratio >= self.MIN_TONE_RATIO
        if self.open:
            if self.dev_hz < self.CLOSE_DEV_HZ or not stable:
                self.open = False
                self._open_cnt = 0
        elif self.dev_hz >= self.OPEN_DEV_HZ and stable:
            self._open_cnt += 1
            if self._open_cnt >= 2:      # two windows ~= 0.5 s persistence
                self.open = True
                self._open_cnt = 0
        else:
            self._open_cnt = 0

    def reset(self):
        self._buf = np.zeros(0, dtype=np.float64)
        self.open = False
        self.dev_hz = 0.0
        self.ratio = 0.0
        self._open_cnt = 0


class NbfmDemod:
    """Narrowband FM for NOAA Weather Radio (5 kHz deviation, 25 kHz grid)."""

    IF_RATE = 240_000

    def __init__(self, fs, lo_offset_hz):
        self.fs = float(fs)
        self.lo_offset = float(lo_offset_hz)
        self.nco = NcoMixer(self.lo_offset, self.fs)
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)
        # channel power tap (pre-discriminator): discriminator noise explodes
        # on channel-less input, so squelch level must come from IF power
        self.ptap = Decimator(5, 8_000, self.IF_RATE, numtaps=129)
        # channel filter: Carson bandwidth 2*(5+3.4) ~ 17 kHz
        self.dec2 = Decimator(5, 8_000, self.IF_RATE, numtaps=129)  # -> 48 kSPS
        # voice band shaping: 3.4 kHz low-pass + 300 Hz high-pass
        self.lpf_zi = np.zeros(128)
        self.lpf_b = lowpass_taps(129, 3_400, AUDIO_RATE)
        self.hpf_b, self.hpf_a = signal.butter(2, 300.0 / (AUDIO_RATE / 2.0),
                                               "high")
        self.hpf_zi = np.zeros(2)
        self._agc = 0.3
        self.last_chan_db = None
        self._last = np.complex64(0)
        # CTCSS tap: sees the sub-audible band the voice high-pass removes
        self.ctcss = CtcssDetector(AUDIO_RATE)

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        pt = self.ptap.process(x)
        self.last_chan_db = 10.0 * float(np.log10(
            np.mean(np.abs(pt) ** 2) + 1e-12))
        z = np.concatenate(([self._last], x))
        self._last = x[-1]
        d = np.angle(z[1:] * np.conj(z[:-1]))
        audio = d * (self.IF_RATE / (2.0 * np.pi * NBFM_DEV_HZ))
        y = self.dec2.process(audio.astype(np.complex64))
        audio = y.real
        audio = signal.lfilter(self.lpf_b, 1.0, audio, zi=self.lpf_zi)[0]
        self.ctcss.process(audio)   # sub-audible tap, before the 300 Hz HPF
        audio = signal.lfilter(self.hpf_b, self.hpf_a, audio,
                               zi=self.hpf_zi)[0]
        # slow AGC lifts typical voice levels toward ~0.7 FS
        peak = float(np.max(np.abs(audio)) + 1e-9)
        self._agc = 0.999 * self._agc + 0.001 * peak
        gain = min(0.7 / self._agc, 30.0)
        return to_int16(audio * gain)

    def reset(self):
        self.nco.set_freq(self.lo_offset)
        self.dec1.reset()
        self.dec2.reset()
        self.ptap.reset()
        self.lpf_zi[:] = 0
        self.hpf_zi[:] = 0
        self._agc = 0.3
        self._last = np.complex64(0)
        tone = self.ctcss.tone_hz
        self.ctcss = CtcssDetector(AUDIO_RATE)
        self.ctcss.set_tone(tone)


class AmDemod:
    """AM demodulator (envelope detection).

    Two operating modes:
      * lo_offset_hz=0     - direct-sampling input, station mixed to exact DC
                             (v3 Q-branch, used for the AM broadcast band)
      * lo_offset_hz>0     - quadrature input with offset tuning (airband):
                             the dongle tunes below the channel and the NCO
                             shifts the station back to DC here
    """

    IF_RATE = 240_000

    def __init__(self, fs, lo_offset_hz=0.0, channel_bw_hz=7_500):
        self.fs = float(fs)
        self.lo_offset = float(lo_offset_hz)
        self.nco = NcoMixer(self.lo_offset, self.fs)
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)  # -> 240 kSPS
        # sharp channel filter rejects the 25 kHz-spaced neighbours
        self.dec2 = Decimator(5, channel_bw_hz, self.IF_RATE, numtaps=321)
        # DC blocker (~30 Hz high-pass) removes residual DC spike
        alpha = np.exp(-2.0 * np.pi * 30.0 / AUDIO_RATE)
        self.dc_b = np.array([1.0, -1.0])
        self.dc_a = np.array([1.0, -alpha])
        self.dc_zi = np.zeros(1)
        self._agc = 0.5
        self.last_chan_db = None

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        x = self.dec2.process(x)
        self.last_chan_db = 10.0 * float(np.log10(
            np.mean(np.abs(x) ** 2) + 1e-12))
        env = np.abs(x)                       # envelope of (1 + m(t))
        env, self.dc_zi = signal.lfilter(self.dc_b, self.dc_a, env, zi=self.dc_zi)
        # slow AGC so carrier level maps to ~0.8 FS regardless of signal strength
        peak = float(np.max(np.abs(env)) + 1e-9)
        self._agc = 0.999 * self._agc + 0.001 * peak
        gain = 0.8 / self._agc
        audio = env * gain
        return to_int16(audio)

    def reset(self):
        self.nco.set_freq(self.lo_offset)
        self.dec1.reset()
        self.dec2.reset()
        self.dc_zi[:] = 0


class SsbDemod:
    """Single-sideband demodulator (USB / LSB) — phasing method.

    Signal path: offset-tuned capture -> channel filter -> BFO fine shift
    -> one-sided complex bandpass selecting the wanted sideband -> real
    part = audio. Usable on the airband (military / utility SSB users) and
    on the AM band (shortwave SSB via v3 direct sampling).

    The BFO is a clarifier: with the dial on the suppressed carrier, a
    positive BFO value shifts the passband DOWN (for a station tuned a
    little too high), negative shifts it up. Range +/-3 kHz.
    """

    IF_RATE = 240_000
    VOICE_LO_HZ = 275.0     # lower edge of the selected sideband
    VOICE_HI_HZ = 3_175.0   # upper edge (275..3175 from LPF @1450 + shift 1725)

    def __init__(self, fs, lo_offset_hz, side="usb", bfo_hz=0.0,
                 channel_bw_hz=6_800):
        self.fs = float(fs)
        self.lo_offset = float(lo_offset_hz)
        self.side = "lsb" if str(side).lower() == "lsb" else "usb"
        self.nco = NcoMixer(self.lo_offset, self.fs)
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)   # -> 240 kSPS
        # wide enough that a full +/-3 kHz BFO shift keeps the voice band
        # inside the channel filter, still rejecting 25 kHz neighbours
        self.dec2 = Decimator(5, channel_bw_hz, self.IF_RATE, numtaps=321)
        self._bfo_ph = 0.0
        self.set_bfo(bfo_hz)
        # one-sided voice bandpass: real LPF (pass +/-1450 Hz @ 48 kSPS)
        # modulated up (USB) or down (LSB) by 1725 Hz -> complex taps
        base = lowpass_taps(255, 1_450.0, AUDIO_RATE).astype(np.float64)
        nn = np.arange(len(base), dtype=np.float64)
        sgn = 1.0 if self.side == "usb" else -1.0
        self.sb_b = (base * np.exp(
            sgn * 1j * 2.0 * np.pi * 1_725.0 * nn / AUDIO_RATE))
        self.sb_zi = np.zeros(len(self.sb_b) - 1, dtype=np.complex128)
        self._agc = 0.5
        self.last_chan_db = None

    def set_bfo(self, hz):
        """Clarifier offset in Hz; phase-continuous (no audio click)."""
        self.bfo_hz = float(np.clip(float(hz), -3_000.0, 3_000.0))
        self._bfo_inc = 2.0 * np.pi * self.bfo_hz / AUDIO_RATE

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        x = self.dec2.process(x)
        self.last_chan_db = 10.0 * float(np.log10(
            np.mean(np.abs(x) ** 2) + 1e-12))
        # BFO fine shift, phase-continuous across blocks
        n = len(x)
        ph = self._bfo_ph + self._bfo_inc * np.arange(n, dtype=np.float64)
        self._bfo_ph = float((self._bfo_ph + self._bfo_inc * n) % (2.0 * np.pi))
        xb = x * np.exp(-1j * ph).astype(np.complex64)
        y, self.sb_zi = signal.lfilter(self.sb_b, 1.0,
                                       xb.astype(np.complex128), zi=self.sb_zi)
        audio = y.real * 2.0
        # slow AGC like the AM path (SSB level wanders with the operator)
        peak = float(np.max(np.abs(audio)) + 1e-9)
        self._agc = 0.999 * self._agc + 0.001 * peak
        return to_int16(audio * (0.8 / self._agc))

    def reset(self):
        self.nco.set_freq(self.lo_offset)
        self.dec1.reset()
        self.dec2.reset()
        self.sb_zi[:] = 0
        self._bfo_ph = 0.0
        self._agc = 0.5


def to_int16(audio, scale=0.9):
    """Float audio in ~[-1, 1] -> soft-limited int16 mono block."""
    x = np.asarray(audio, dtype=np.float64)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 1.0:
        x = np.tanh(x / peak) * peak  # gentle limiter, preserves quiet detail
    return (np.clip(x * scale, -1.0, 1.0) * 32767.0).astype(np.int16)


# --------------------------------------------------------------------------
# Spectrum analyzer
# --------------------------------------------------------------------------
def downsample_max(arr, n_out):
    pad = (-len(arr)) % n_out
    if pad:
        arr = np.concatenate([arr, np.full(pad, arr[-1])])
    return arr.reshape(-1, n_out).max(axis=0)


class PsdComputer:
    """Welch-averaged PSD over the captured window, reduced to 512 bins."""

    N_BINS = 512
    NPERSEG = 8192

    def process(self, raw, fs, center_hz, station_hz):
        f, pxx = signal.welch(
            raw, fs=fs, window="hann", nperseg=self.NPERSEG,
            noverlap=self.NPERSEG // 2, return_onesided=False, detrend=False,
        )
        f = np.fft.fftshift(f)
        p = 10.0 * np.log10(np.fft.fftshift(pxx) + 1e-14)
        psd = downsample_max(p.astype(np.float32), self.N_BINS)
        axis = np.linspace(f[0], f[-1], self.N_BINS)  # offset from center
        floor = float(np.percentile(psd, 20))
        # signal power near the tuned station (station appears at its offset)
        off = station_hz - center_hz
        sel = (axis >= off - 60e3) & (axis <= off + 60e3)
        sig = float(psd[sel].mean()) if sel.any() else floor
        return {
            "psd_db": [round(float(v), 1) for v in psd],
            "axis_hz0": float(axis[0]),
            "axis_hz1": float(axis[-1]),
            "center_hz": float(center_hz),
            "span_hz": float(fs),
            "floor_db": round(floor, 1),
            "sig_db": round(sig, 1),
        }
