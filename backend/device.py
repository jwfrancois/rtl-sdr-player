"""SDR device layer.

Provides a uniform interface over:
  * RtlSdrDevice  - the real RTL2832U dongle via pyrtlsdr (librtlsdr)
  * SimDevice     - a synthetic over-the-air simulator (no hardware needed)

open_device() performs detection and never raises: it returns
(device, info) where info describes what was opened, or why it failed.
"""
import math
import os
import platform
import time

import numpy as np

from . import rds as rds_mod

IS_WINDOWS = platform.system() == "Windows"


# --------------------------------------------------------------------------
# Real hardware
# --------------------------------------------------------------------------
class RtlSdrDevice:
    is_real = True

    def __init__(self, index=0):
        from rtlsdr import RtlSdr  # deferred import: gives friendly errors upstream

        self.index = index
        self.sdr = RtlSdr(index=index)
        self._open = True

    # -- identity ----------------------------------------------------------
    @property
    def name(self):
        try:
            return (self.sdr.get_device_name() or "RTL2832U").strip() or "RTL2832U"
        except Exception:
            return "RTL2832U"

    # -- configuration -----------------------------------------------------
    def set_center_freq(self, freq_hz):
        self.sdr.center_freq = freq_hz

    def set_sample_rate(self, rate):
        self.sdr.sample_rate = rate

    def set_gain(self, gain_db=None):
        """gain_db=None -> hardware AGC; else a value from get_gains()."""
        if gain_db is None:
            self.sdr.gain = "auto"
        else:
            self.sdr.gain = float(gain_db)

    def get_gains(self):
        try:
            return [round(g / 10.0, 1) for g in self.sdr.get_gains()]
        except Exception:
            return []

    def set_direct_sampling(self, mode):
        """mode: 'disabled' | 'i' | 'q'  ('q' is used for AM / HF on the v3)"""
        self.sdr.set_direct_sampling(mode)

    def set_bias_tee(self, on):
        try:
            self.sdr.set_bias_tee(bool(on))
        except Exception:
            pass  # older librtlsdr builds lack rtlsdr_set_bias_tee

    # -- streaming ---------------------------------------------------------
    def read_iq(self, num_samples):
        """Returns complex64 IQ block normalized to roughly [-1, 1]."""
        return self.sdr.read_samples(num_samples)

    def close(self):
        if self._open:
            try:
                self.sdr.close()
            except Exception:
                pass
            self._open = False


# --------------------------------------------------------------------------
# Simulator (used when no dongle is present, e.g. for self-tests / demos)
# --------------------------------------------------------------------------
class SimDevice:
    is_real = False

    # Synthetic stations: (absolute Hz, amplitude, kind)
    #   kind "wfm"  - broadcast FM music-like modulation
    #   kind "rds"  - wfm + 19 kHz pilot + RDS subcarrier (decoder test)
    #   kind "am"   - AM voice (airband quadrature or AM-band direct)
    #   kind "nbfm" - narrowband FM voice (NOAA weather)
    STATIONS = [
        (88.5e6, 0.55, "wfm"), (92.3e6, 0.40, "wfm"),
        (96.1e6, 0.72, "rds"),
        (99.9e6, 0.50, "wfm"), (103.7e6, 0.62, "wfm"), (107.1e6, 0.34, "wfm"),
        (640e3, 0.50, "am"), (820e3, 0.66, "am"),
        (1010e3, 0.42, "am"), (1330e3, 0.56, "am"),
        (121.5e6, 0.50, "am"), (122.8e6, 0.42, "am"), (123.1e6, 0.38, "am"),
        (162.400e6, 0.45, "nbfm"), (162.550e6, 0.55, "nbfm"),
    ]

    RDS_PI = 0x2A5B
    RDS_PS = "SIM-RDS"
    RDS_RT = "RTL-SDR Player 1.1 - RDS ok - USA 75us deemph"

    def __init__(self):
        self.center = 98.3e6
        self.rate = 2_400_000
        self._t = 0.0  # continuous time base (seconds)
        self.direct_sampling = "disabled"
        self._rng = np.random.default_rng(1234)
        self._rds = rds_mod.RdsEncoder(
            pi=self.RDS_PI, ps=self.RDS_PS, radiotext=self.RDS_RT,
            pty=9, ms=1, af_codes=(87, 124))  # 96.2 / 99.9 MHz alternates
        self._acc = {}  # station freq -> [pilot phase acc, rds phase acc, nbfm acc]

    @property
    def name(self):
        return "Simulated RF spectrum (no hardware)"

    def set_center_freq(self, freq_hz):
        self.center = float(freq_hz)

    def set_sample_rate(self, rate):
        self.rate = int(rate)

    def set_gain(self, gain_db=None):
        pass

    def get_gains(self):
        return []

    def set_direct_sampling(self, mode):
        self.direct_sampling = mode

    def set_bias_tee(self, on):
        pass

    def _acc_for(self, freq):
        acc = self._acc.get(freq)
        if acc is None:
            acc = self._acc[freq] = [0.0, 0.0, 0.0]
        return acc

    def read_iq(self, num_samples):
        n = int(num_samples)
        fs = float(self.rate)
        t0 = self._t
        t = t0 + np.arange(n, dtype=np.float64) / fs
        self._t = t0 + n / fs

        is_am_band = self.direct_sampling in ("q", "i", 1, 2)
        two_pi = 2.0 * math.pi

        out = (self._rng.standard_normal(n)
               + 1j * self._rng.standard_normal(n)).astype(np.complex64) * 0.035

        for freq, amp, kind in self.STATIONS:
            off = freq - self.center
            if is_am_band:
                if kind != "am":
                    continue
                # direct sampling: the tuned station lands at DC naturally
            elif abs(off) > fs * 0.47:
                continue  # station outside the captured window

            # Three-tone modulation unique per station
            k = int(abs(freq) / 1e6)
            m1 = two_pi * (220.0 + 40.0 * k) * t
            m2 = two_pi * (331.0 + 17.0 * k) * t + 1.1
            m3 = two_pi * (523.0 + 11.0 * k) * t + 2.3
            audio = (np.sin(m1) + 0.5 * np.sin(m2) + 0.25 * np.sin(m3)) / 1.75
            lfo = 0.75 + 0.25 * np.sin(two_pi * 0.13 * t + k)

            if kind == "am":
                # AM voice: carrier + modulated sidebands
                sig = (1.0 + 0.5 * audio * lfo) * np.exp(
                    1j * (two_pi * off * t)).astype(np.complex64)
            elif kind == "nbfm":
                # +/-5 kHz deviation, integrated properly for the NBFM scale
                acc = self._acc_for(freq)
                c = np.cumsum(5000.0 * audio * lfo)
                ph = two_pi * off * t + acc[2] + two_pi * c / fs
                acc[2] = (acc[2] + two_pi * float(c[-1]) / fs) % two_pi
                sig = np.exp(1j * ph).astype(np.complex64)
            elif kind == "rds":
                # wfm music + 9% pilot + RDS subcarrier (DSB-SC, +/-2 kHz)
                acc = self._acc_for(freq)
                cp = np.cumsum(6750.0 * np.sin(two_pi * 19000.0 * t))
                cr = np.cumsum(self._rds.subcarrier_dev(t, 0.0))
                ph = (two_pi * off * t + 5.2 * audio * lfo
                      + acc[0] + two_pi * cp / fs
                      + acc[1] + two_pi * cr / fs)
                acc[0] = (acc[0] + two_pi * float(cp[-1]) / fs) % two_pi
                acc[1] = (acc[1] + two_pi * float(cr[-1]) / fs) % two_pi
                sig = np.exp(1j * ph).astype(np.complex64)
            else:  # plain wfm
                phase = (two_pi * off * t + 5.2 * audio * lfo)
                sig = np.exp(1j * phase).astype(np.complex64)
            out += (amp * sig).astype(np.complex64)

        return out.astype(np.complex64)

    def close(self):
        pass


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def _import_error_hint():
    if IS_WINDOWS:
        return (
            "pyrtlsdr is installed but the native librtlsdr DLLs were not found. "
            "Copy rtlsdr.dll and libusb-1.0.dll into this app folder "
            "(see README.md, step 2)."
        )
    return (
        "pyrtlsdr is installed but librtlsdr was not found. "
        "Install it with your package manager, e.g. sudo apt install librtlsdr-dev rtl-sdr"
    )


def open_device(prefer_sim=False):
    """Returns (device, info). Never raises when sim_fallback is allowed."""
    from . import config as cfg

    if not prefer_sim:
        try:
            dev = RtlSdrDevice(0)
            info = {
                "mode": "hardware",
                "device": dev.name,
                "backend": "pyrtlsdr / librtlsdr",
                "note": "RTL2832U detected and streaming.",
            }
            return dev, info
        except ImportError:
            hint = _import_error_hint()
        except Exception as exc:
            hint = _usb_error_hint(exc)
    else:
        hint = "Simulation mode requested."

    info = {
        "mode": "sim",
        "device": "Simulated RF spectrum",
        "backend": "numpy simulator",
        "note": hint if not prefer_sim else "Running with synthetic signals.",
        "error": hint if not prefer_sim else None,
    }
    return SimDevice(), info


def _usb_error_hint(exc):
    msg = str(exc)
    if IS_WINDOWS and ("libusb" in msg.lower() or "open" in msg.lower()
                       or "access" in msg.lower() or "device" in msg.lower()):
        return (
            "Could not open the RTL2832U. On Windows this almost always means the "
            "WinUSB driver is not bound to the dongle yet: run Zadig, pick Options > "
            "'List All Devices', select 'Bulk-In, Interface (Interface 0)' and install "
            "the WinUSB driver (see README.md, step 1). If another SDR program "
            "(SDR#, SDR++, etc.) is running, close it first."
        )
    if "No such file" in msg or "not found" in msg.lower():
        return (
            "No RTL2832U found on USB. Check the plug, and on Linux make sure the "
            "DVB kernel driver is not holding the stick: "
            "sudo modprobe -r dvb_usb_rtl28xxu"
        )
    return f"Could not open the dongle: {msg}"
