"""Standalone test: RdsDecoder against a synthetic RDS+Pilot FM signal.

Models the app's signal chain exactly:
    x[n] = exp(j * 2*pi * cumsum(dev)/IF)            complex IF at 240 kSPS
    d[n] = angle(x[n] * conj(x[n-1])) = 2*pi*dev/IF   discriminator output
where dev (Hz) = 75e3*audio + 6750*sin(2*pi*19e3*t*(1+ppm))
                 + 2000*cos(2*pi*57e3*t*(1+ppm) + pi*b(t)).
PilotWatch and RdsDecoder both see the discriminator output d (the pilot is
a clean 19 kHz tone in the demodulated MPX, just like real receivers see).
"""
import sys
import time

import numpy as np

sys.path.insert(0, "/home/z/my-project/download/rtl-sdr-player")

from backend.rds import RdsDecoder, RdsEncoder  # noqa: E402

IF = 240_000
BLOCK = 26_214          # ~109 ms (matches app: 2.4 MSPS / 10)
PPM = 37.0


class PilotWatch:
    """19 kHz pilot tracker (same algorithm as backend/dsp.py)."""

    def __init__(self, fs):
        self.fs = float(fs)
        self.n0 = 0
        self._prev_phase = None
        self._freq_off = 0.0
        self.present = False
        self.rel_db = -99.0

    def process(self, d):
        """d: real discriminator output (rad/sample) at fs."""
        n = len(d)
        w = np.exp(-2j * np.pi * 19000.0 * (self.n0 + np.arange(n)) / self.fs)
        self.n0 += n
        g = np.dot(d, w)
        power = (abs(g) ** 2) * 4.0 / (n * n)
        sig = float(np.mean(np.asarray(d) ** 2)) + 1e-12
        self.rel_db = 10.0 * np.log10(power / sig)
        self.present = self.rel_db > -25.0
        ph = float(np.angle(g))
        if self._prev_phase is not None and self.present:
            d = (ph - self._prev_phase + np.pi) % (2 * np.pi) - np.pi
            df = d / (2.0 * np.pi * n / self.fs)
            if abs(df) < 50.0:
                self._freq_off = 0.8 * self._freq_off + 0.2 * df
        self._prev_phase = ph

    @property
    def ppm(self):
        return self._freq_off / 19000.0 * 1e6


def run():
    enc = RdsEncoder(pi=0x2A5B, ps="SIM-RDS",
                     radiotext="RTL-SDR Player test - RDS ok - 75us deemph US",
                     pty=9, af_codes=(87, 124))
    dec = RdsDecoder(IF)
    pilot = PilotWatch(IF)
    rng = np.random.default_rng(11)
    t_abs = 0.0
    phi_acc = 0.0
    t0 = time.time()
    for it in range(120):  # ~13 s of signal
        t = t_abs + np.arange(BLOCK) / IF
        t_abs += BLOCK / IF
        audio = 0.6 * np.sin(2 * np.pi * 350.0 * t + 0.4) \
            + 0.4 * np.sin(2 * np.pi * 1020.0 * t)
        dev = (75000.0 * audio
               + 6750.0 * np.sin(2 * np.pi * 19000.0 * (1 + PPM * 1e-6) * t)
               + enc.subcarrier_dev(t, PPM))
        # complex IF (for reference only) + discriminator output
        phi = 2.0 * np.pi * np.cumsum(dev) / IF + phi_acc
        phi_acc = float(phi[-1])
        x = np.exp(1j * phi).astype(np.complex64)
        d = np.diff(phi.astype(np.float64))
        d = np.concatenate(([d[0]], d))  # length match; first-sample glitch ok
        d_noisy = d + 0.02 * rng.standard_normal(BLOCK)

        pilot.process(d_noisy)
        if pilot.present:
            dec.set_ppm(pilot.ppm)
        dec.process(d_noisy)
        snap = dec.snapshot()
        if it in (10, 40, 119):
            print(f"iter {it:3d}  pilot={pilot.present} rel={pilot.rel_db:5.1f}dB "
                  f"ppm={pilot.ppm:+7.2f}  locked={snap['locked']} "
                  f"groups={snap['groups']} err={snap['errors']} "
                  f"pi={snap['pi']} ps={snap['ps']!r}")
    wall = time.time() - t0
    snap = dec.snapshot()
    print(f"\nprocessing wall time: {wall:.2f} s for 13.1 s of signal "
          f"({13.1 / wall:.1f}x realtime)")
    print("AF:", snap["af_mhz"])
    print("PTY:", snap["pty"])
    print("RT:", snap["radiotext"])
    print("CLOCK:", snap["clock"])
    ok = (snap["pi"] == "2A5B" and snap["ps"] == "SIM-RDS"
          and snap["pty"] == "Top 40" and snap["groups"] > 50)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
