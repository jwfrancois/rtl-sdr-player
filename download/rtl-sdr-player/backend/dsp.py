"""DSP engine: frequency shifting, decimation, FM/AM demodulation, PSD.

All processing is vectorized numpy/scipy operating on block streams, sized to
run comfortably faster than real time on a desktop CPU.

Signal path (FM broadcast):
    IQ @ 2.4 MSPS -> NCO shift (station to DC) -> decimate x10 -> 240 kSPS
    -> polar discriminator -> audio LPF + decimate x5 -> 48 kHz
    -> de-emphasis IIR -> int16 mono

Signal path (AM broadcast, direct sampling):
    IQ @ 2.4 MSPS -> NCO shift -> decimate x10 -> 240 kSPS -> decimate x5
    (sharp 7.5 kHz channel filter) -> 48 kSPS -> envelope detect
    -> DC blocker + AGC -> int16 mono
"""
import numpy as np
from scipy import signal

AUDIO_RATE = 48_000
BLOCK = 262_144  # capture block (~109 ms at 2.4 MSPS)

# FM deviations used to normalize demodulated audio to +/-1.0 full scale
WFM_DEV_HZ = 75_000


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
    """Single-pole FM de-emphasis (75 us Americas/Korea, 50 us most others)."""

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


# --------------------------------------------------------------------------
# Demodulators
# --------------------------------------------------------------------------
class WfmDemod:
    """Wideband FM broadcast demodulator (mono output)."""

    IF_RATE = 240_000

    def __init__(self, fs, lo_offset_hz, de_emphasis_us):
        self.fs = float(fs)
        self.lo_offset = float(lo_offset_hz)
        self.nco = NcoMixer(-self.lo_offset, self.fs)  # station sits at +offset
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)   # -> 240 kSPS
        self.dec2 = Decimator(5, 15_500, self.IF_RATE, numtaps=129)  # -> 48 kHz
        self.deemph = DeEmphasis(de_emphasis_us)
        self._last = np.complex64(0)

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        # polar discriminator with 1-sample memory across blocks
        z = np.concatenate(([self._last], x))
        self._last = x[-1]
        d = np.angle(z[1:] * np.conj(z[:-1]))
        audio = d * (self.IF_RATE / (2.0 * np.pi * WFM_DEV_HZ))
        audio = self.dec2.process(audio.astype(np.complex64)).real
        audio = self.deemph.process(audio.astype(np.float64))
        return to_int16(audio)

    def reset(self):
        self.nco.set_freq(-self.lo_offset)
        self.dec1.reset()
        self.dec2.reset()
        self.deemph.reset()
        self._last = np.complex64(0)


class AmDemod:
    """AM demodulator for direct-sampling input (envelope detection)."""

    IF_RATE = 240_000

    def __init__(self, fs):
        self.fs = float(fs)
        self.nco = NcoMixer(0.0, self.fs)  # station mixed to exact DC
        self.dec1 = Decimator(10, 120_000, self.fs, numtaps=129)  # -> 240 kSPS
        # sharp channel filter: pass 7.5 kHz, reject 10 kHz-spaced neighbors
        self.dec2 = Decimator(5, 7_500, self.IF_RATE, numtaps=321)  # -> 48 kSPS
        # DC blocker (~30 Hz high-pass) removes residual DC spike
        alpha = np.exp(-2.0 * np.pi * 30.0 / AUDIO_RATE)
        self.dc_b = np.array([1.0, -1.0])
        self.dc_a = np.array([1.0, -alpha])
        self.dc_zi = np.zeros(1)
        self._agc = 0.5

    def process(self, raw):
        x = self.nco.process(raw)
        x = self.dec1.process(x)
        x = self.dec2.process(x)
        env = np.abs(x)                       # envelope of (1 + m(t))
        env, self.dc_zi = signal.lfilter(self.dc_b, self.dc_a, env, zi=self.dc_zi)
        # slow AGC so carrier level maps to ~0.8 FS regardless of signal strength
        peak = float(np.max(np.abs(env)) + 1e-9)
        self._agc = 0.999 * self._agc + 0.001 * peak
        gain = 0.8 / self._agc
        audio = env * gain
        return to_int16(audio)

    def reset(self):
        self.nco.set_freq(0.0)
        self.dec1.reset()
        self.dec2.reset()
        self.dc_zi[:] = 0


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
