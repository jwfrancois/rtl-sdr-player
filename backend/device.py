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
import struct
import sys
import time

import numpy as np

from . import rds as rds_mod

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# --------------------------------------------------------------------------
# Linux USB probe (v1.7.2): "the player does not see my dongle" on Ubuntu
#
# Four causes cover essentially every case, in this order:
#   1. the dongle is not actually on the bus (cable/port/power)
#   2. the DVB TV kernel driver (dvb_usb_rtl28xxu) claimed it first
#   3. the /dev/bus/usb node is not exposed to this process (Docker without
#      --device=/dev/bus/usb, or a restrictive udev setup)
#   4. the user has no read/write permission on the node (udev rules)
#
# The probe reads sysfs directly — no lsusb / udevadm / sudo needed — and
# works both on a bare host and inside a container (sysfs is shared with
# the host, so driver claims and device presence are visible either way).
# --------------------------------------------------------------------------
RTL_USB_VID = "0bda"
RTL_USB_PIDS = {"2832", "2838"}     # RTL2832U / RTL2838UHIDIR (v3 sticks)
DVB_CLAIM_DRIVERS = {"dvb_usb_rtl28xxu", "dvb_usb_rtl2832u"}


def _sysfs_read(path):
    """Small text file from sysfs -> stripped string, or None. Never raises."""
    try:
        with open(path, "r") as fh:
            return fh.read().strip().lower()
    except OSError:
        return None


def probe_linux_usb(sys_root="/sys/bus/usb/devices", mod_root="/sys/module",
                    dev_root="/dev/bus/usb"):
    """Best-effort host USB probe for an RTL2832U dongle (Linux only).

    Returns a dict with structured findings plus a single human-readable
    `hint` describing what (if anything) is wrong and the exact fix.
    Designed to be embedded in /api/status so the browser can show it.
    Never raises. The paths are parameters so tests can feed a fake tree.
    """
    rep = {"os": "linux", "present": False, "devices": [],
           "driver_bound": None, "dvb_module_loaded": False,
           "devnode": None, "devnode_ok": None, "hint": None}
    if not IS_LINUX and not os.path.isdir(sys_root):
        rep["os"] = platform.system().lower()
        return rep
    try:
        for entry in sorted(os.listdir(sys_root)):
            try:
                full = os.path.join(sys_root, entry)
                if not os.path.isdir(full):
                    continue
                vid = _sysfs_read(os.path.join(full, "idVendor"))
                if vid != RTL_USB_VID:
                    continue
                pid = _sysfs_read(os.path.join(full, "idProduct"))
                if pid in RTL_USB_PIDS:
                    rep["devices"].append({"sysfs": entry, "vid": vid,
                                           "pid": pid})
            except OSError:
                continue          # one odd entry must not kill the probe
    except OSError:
        # sysfs unavailable (restricted container / exotic kernel): say so
        # instead of staying silent — the UI banner needs a verdict
        rep["hint"] = ("USB probe unavailable (no sysfs in this environment): "
                       "the USB bus cannot be checked from here. On a normal "
                       "Ubuntu host this never happens.")
        return rep
    rep["present"] = bool(rep["devices"])

    if not rep["present"]:
        rep["hint"] = (
            "No RTL2832U dongle found on the USB bus. Replug the stick, try "
            "another port or cable, and check `lsusb` for a Realtek entry "
            "(0bda:2832 / 0bda:2838). On a server, avoid unpowered hubs.")
        return rep

    dev0 = rep["devices"][0]["sysfs"]   # first match, e.g. "3-2"

    # 1) DVB TV driver bound to one of its interfaces ("3-2:1.0")
    try:
        for ifdir in sorted(os.listdir(sys_root)):
            if not ifdir.startswith(dev0 + ":"):
                continue
            drv = os.path.join(sys_root, ifdir, "driver")
            if os.path.islink(drv):
                rep["driver_bound"] = os.path.basename(os.readlink(drv))
                break
    except OSError:
        pass
    rep["dvb_module_loaded"] = os.path.isdir(
        os.path.join(mod_root, "dvb_usb_rtl28xxu"))
    if rep["driver_bound"] in DVB_CLAIM_DRIVERS:
        rep["hint"] = (
            "The dongle is on the bus, but the DVB TV kernel driver "
            "(dvb_usb_rtl28xxu) has claimed it - the #1 cause on Ubuntu. "
            "Free it now: sudo modprobe -r dvb_usb_rtl28xxu rtl2832 r820t "
            "and make it permanent with: sudo bash linux-setup.sh "
            "(blacklist + udev rules). Works the same for Docker: the "
            "blacklist belongs on the HOST.")
        return rep
    if rep["dvb_module_loaded"] and rep["driver_bound"] is None:
        rep["hint"] = (
            "The DVB TV kernel module (dvb_usb_rtl28xxu) is loaded and will "
            "claim the dongle at the next replug. Make it permanent: "
            "sudo bash linux-setup.sh (blacklist + udev rules), or right "
            "now: sudo modprobe -r dvb_usb_rtl28xxu")
        return rep

    # 2) device node visible?
    def _node_num(v):
        try:
            return "%03d" % int(str(v), 0)   # decimal (sysfs) or 0x-prefixed
        except ValueError:
            return None

    busnum = _sysfs_read(os.path.join(sys_root, dev0, "busnum"))
    devnum = _sysfs_read(os.path.join(sys_root, dev0, "devnum"))
    node = None
    b, d = _node_num(busnum), _node_num(devnum)
    if b and d:
        node = os.path.join(dev_root, b, d)
    rep["devnode"] = node
    if node is None or not os.path.exists(node):
        rep["devnode_ok"] = False
        rep["hint"] = (
            "Dongle IS on the USB bus, but its /dev/bus/usb device node is "
            "not visible to this process. Under Docker, pass the bus "
            "through: --device=/dev/bus/usb (docker-compose.yml already "
            "does). On a bare host, replug the stick or check that udev "
            "created the node.")
        return rep

    # 3) read/write permission on the node?
    ok = os.access(node, os.R_OK | os.W_OK)
    rep["devnode_ok"] = ok
    if not ok:
        rep["hint"] = (
            "Dongle present, but this user has no read/write access to "
            + node + ". Install udev rules: sudo bash linux-setup.sh "
            "(or add your user to the plugdev group and log out/in).")
        return rep

    rep["hint"] = (
        "USB side looks healthy: dongle present, no DVB driver bound, "
        "device node accessible. If the player still cannot open it, "
        "another SDR program may be holding the stick, or librtlsdr failed "
        "to load - see the error text below the header.")
    return rep


_probe_cache = {"t": 0.0, "rep": None}


def probe_linux_usb_cached(ttl=10.0):
    """Cached probe for /api/status (polled every few seconds)."""
    now = time.monotonic()
    if _probe_cache["rep"] is None or now - _probe_cache["t"] > ttl:
        _probe_cache["rep"] = probe_linux_usb()
        _probe_cache["t"] = now
    return _probe_cache["rep"]

# --------------------------------------------------------------------------
# Windows DLL discovery (THE fix for "DLLs are in the folder but not found")
#
# Since Python 3.8, ctypes no longer searches the application folder, the
# current working directory or PATH when loading native DLLs. pyrtlsdr does
# a bare `WinDLL("rtlsdr.dll")`, so DLLs dropped next to app.py are silently
# invisible. Registering the app folder with os.add_dll_directory() (plus a
# PATH prepend for good measure) makes "copy the DLLs into the app folder"
# actually work on Python 3.8+.
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))          # backend/
_APP_ROOT = os.path.dirname(_HERE)                          # folder w/ app.py
_DLL_CANDIDATE_DIRS = (
    _APP_ROOT,
    os.path.join(_APP_ROOT, "dlls"),
    os.path.join(_APP_ROOT, "drivers"),
    _HERE,
)
_DLL_FILE_NAMES = ("rtlsdr.dll", "librtlsdr.dll", "libusb-1.0.dll")


def _register_windows_dll_dirs():
    """Register every candidate folder that actually contains a DLL.

    Returns the os.add_dll_directory() handles (kept alive on module level
    so the registrations last for the process lifetime).
    """
    handles = []
    if not IS_WINDOWS:
        return handles
    for d in _DLL_CANDIDATE_DIRS:
        try:
            if not any(os.path.isfile(os.path.join(d, n))
                       for n in _DLL_FILE_NAMES):
                continue
            handles.append(os.add_dll_directory(d))
            # PATH prepend: pyrtlsdr also probes ctypes.util.find_library(),
            # which walks PATH on Windows.
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        except OSError:
            pass
    return handles


_DLL_DIR_HANDLES = _register_windows_dll_dirs()


# --------------------------------------------------------------------------
# Real hardware
# --------------------------------------------------------------------------
class RtlSdrDevice:
    is_real = True

    def __init__(self, index=0):
        from rtlsdr import RtlSdr  # deferred import: gives friendly errors upstream

        self.index = index
        try:
            # pyrtlsdr >= 0.5.0 (also accepts serial_number=)
            self.sdr = RtlSdr(device_index=index)
        except TypeError:
            # older pyrtlsdr releases used index=
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
        """mode: 'disabled' | 'i' | 'q'  ('q' is used for AM / HF on the v3)

        pyrtlsdr's string handling changed across releases (older builds
        accept 'i'/'q', newer ones only 'I'/'Q', and at least 0.4+/0.5
        reject 'disabled' outright - SyntaxError 'invalid value'). The C
        API takes an int, and ints are accepted by every version, so
        normalize to 0/1/2 here and fall back to the string spellings
        only if a build refuses ints (no known build does).
        """
        key = mode.lower() if isinstance(mode, str) else mode
        code = {"disabled": 0, "i": 1, "q": 2, 0: 0, 1: 1, 2: 2}.get(key)
        if code is None:
            raise ValueError(
                "direct sampling mode must be disabled/i/q, got %r" % (mode,))
        try:
            self.sdr.set_direct_sampling(code)
            return
        except Exception:
            pass  # exotic fork? try the documented string tokens
        for alt in ({0: "disabled", 1: "I", 2: "Q"}[code],
                    {0: "disabled", 1: "i", 2: "q"}[code]):
            try:
                self.sdr.set_direct_sampling(alt)
                return
            except Exception:
                continue
        raise OSError("set_direct_sampling(%r) rejected by pyrtlsdr/librtlsdr"
                      % (mode,))

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
    #   kind "ssb"  - USB voice, carrier suppressed (analytic envelope)
    #   kind "ssbl" - LSB voice, carrier suppressed (conjugate envelope)
    STATIONS = [
        (88.5e6, 0.55, "wfm"), (92.3e6, 0.40, "wfm"),
        (96.1e6, 0.72, "rds"), (101.7e6, 0.48, "rds"),
        (99.9e6, 0.50, "wfm"), (103.7e6, 0.62, "wfm"), (107.1e6, 0.34, "wfm"),
        (640e3, 0.50, "am"), (820e3, 0.66, "am"),
        (1010e3, 0.42, "am"), (1330e3, 0.56, "am"),
        (121.5e6, 0.50, "am"), (122.8e6, 0.42, "am"), (123.1e6, 0.38, "am"),
        (119.2e6, 0.50, "ssb"), (123.45e6, 0.45, "ssbl"),
        (162.400e6, 0.45, "nbfm"), (162.550e6, 0.55, "nbfm"),
    ]

    RDS_PI = 0x2A5B
    RDS_PS = "SIM-RDS"
    RDS_RT = "RTL-SDR Player 1.4 - RDS ok - USA 75us deemph"

    # Two FM stations carry RDS so the sweep's auto-naming can be verified
    # end-to-end (each with its own PI/PS identity).
    RDS_STATIONS = {
        96_100_000: {"pi": 0x2A5B, "ps": "SIM-RDS", "pty": 9},
        101_700_000: {"pi": 0x1F2C, "ps": "JAZZ-FM", "pty": 14},
    }

    # CTCSS tone (Hz) carried by each simulated NBFM station, at the typical
    # 350 Hz tone deviation. Lets the CTCSS squelch be exercised end-to-end.
    NBFM_TONES = {162_400_000: 123.0, 162_550_000: 141.3}

    def __init__(self):
        self.center = 98.3e6
        self.rate = 2_400_000
        self._t = 0.0  # continuous time base (seconds)
        self.direct_sampling = "disabled"
        self._rng = np.random.default_rng(1234)
        self._rds = rds_mod.RdsEncoder(
            pi=self.RDS_PI, ps=self.RDS_PS, radiotext=self.RDS_RT,
            pty=9, ms=1, af_codes=(87, 124))  # 96.2 / 99.9 MHz alternates
        # one encoder per RDS station (96.1 uses self._rds for compat)
        self._rds_enc = {96_100_000: self._rds}
        for hz, spec in self.RDS_STATIONS.items():
            if hz not in self._rds_enc:
                self._rds_enc[hz] = rds_mod.RdsEncoder(
                    pi=spec["pi"], ps=spec["ps"], radiotext=spec["ps"],
                    pty=spec["pty"], ms=1, af_codes=())
        self._acc = {}  # station freq -> [pilot phase acc, rds phase acc, nbfm acc]

    def _rds_enc_for(self, freq):
        enc = self._rds_enc.get(freq)
        if enc is None:
            enc = self._rds_enc[freq] = self._rds
        return enc

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

        two_pi = 2.0 * math.pi

        out = (self._rng.standard_normal(n)
               + 1j * self._rng.standard_normal(n)).astype(np.complex64) * 0.035

        # Slow modulation tones (audio <= ~600 Hz, LFO 0.13 Hz) are
        # computed on a coarse time grid and linearly interpolated to the
        # capture rate: synthesizing them at 2.4 MHz wastes ~50x of the
        # per-block numpy budget and pushed AM blocks past realtime.
        step = 64
        tc = t[::step]
        nc = len(tc)
        fine = np.arange(n, dtype=np.float64) / step
        coarse_x = np.arange(nc, dtype=np.float64)

        for freq, amp, kind in self.STATIONS:
            off = freq - self.center
            if abs(off) > fs * 0.47:
                continue  # station outside the captured window (all modes;
            # direct sampling included: synthesizing every AM/SSB station
            # regardless of distance made AM blocks ~2.6x slower than
            # realtime once the huge sin/exp arguments hit Payne-Hanek
            # reduction, starving the whole pipeline)

            # Three-tone modulation unique per station
            k = int(abs(freq) / 1e6)
            m1 = two_pi * (220.0 + 40.0 * k) * tc
            m2 = two_pi * (331.0 + 17.0 * k) * tc + 1.1
            m3 = two_pi * (523.0 + 11.0 * k) * tc + 2.3
            audio = np.interp(
                fine, coarse_x,
                (np.sin(m1) + 0.5 * np.sin(m2) + 0.25 * np.sin(m3)) / 1.75)
            lfo = np.interp(fine, coarse_x,
                            0.75 + 0.25 * np.sin(two_pi * 0.13 * tc + k))

            if kind == "am":
                # AM voice: carrier + modulated sidebands. The carrier phase
                # argument is kept inside one turn: the envelope (all the AM
                # detector needs) is unaffected, but numpy's sin/cos stay on
                # the fast path instead of huge-argument reduction.
                ph = (two_pi * off * t) % two_pi
                sig = (1.0 + 0.5 * audio * lfo) * np.exp(
                    1j * ph).astype(np.complex64)
            elif kind in ("ssb", "ssbl"):
                # Single sideband, carrier suppressed. The complex envelope
                # of a USB voice is the ANALYTIC signal (positive-frequency
                # tones only); LSB is its conjugate. Voice tones stay inside
                # the 300-3000 Hz SSB passband on purpose.
                mva1 = np.exp(1j * two_pi * 440.0 * tc)
                mva2 = np.exp(1j * (two_pi * 880.0 * tc + 1.1))
                mva3 = np.exp(1j * (two_pi * 1320.0 * tc + 2.3))
                va_c = (mva1 + 0.5 * mva2 + 0.25 * mva3) / 1.75 * lfo[::step]
                va = (np.interp(fine, coarse_x, va_c.real)
                      + 1j * np.interp(fine, coarse_x, va_c.imag))
                if kind == "ssbl":
                    va = np.conj(va)
                ph = (two_pi * off * t) % two_pi
                sig = (va * np.exp(1j * ph)).astype(np.complex64)
            elif kind == "nbfm":
                # +/-5 kHz deviation, integrated properly for the NBFM scale,
                # plus the station's CTCSS tone at 350 Hz deviation
                acc = self._acc_for(freq)
                dev = 5000.0 * audio * lfo
                tone_hz = self.NBFM_TONES.get(freq)
                if tone_hz:
                    dev = dev + 350.0 * np.sin(two_pi * tone_hz * t)
                c = np.cumsum(dev)
                ph = two_pi * off * t + acc[2] + two_pi * c / fs
                acc[2] = (acc[2] + two_pi * float(c[-1]) / fs) % two_pi
                sig = np.exp(1j * ph).astype(np.complex64)
            elif kind == "rds":
                # wfm music + 9% pilot + RDS subcarrier (DSB-SC, +/-2 kHz)
                enc = self._rds_enc_for(freq)
                acc = self._acc_for(freq)
                cp = np.cumsum(6750.0 * np.sin(two_pi * 19000.0 * t))
                cr = np.cumsum(enc.subcarrier_dev(t, 0.0))
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
_PE_MACHINE = {0x014C: "32-bit (x86)", 0x8664: "64-bit (x64)",
               0xAA64: "64-bit (ARM64)"}


def _pe_machine(path):
    """'64-bit (x64)' / '32-bit (x86)' from a DLL's PE header, or None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
        if len(data) < 0x40 or data[:2] != b"MZ":
            return None
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_off + 6 > len(data):
            return None
        machine = struct.unpack_from("<H", data, pe_off + 4)[0]
        return _PE_MACHINE.get(machine, hex(machine))
    except OSError:
        return None


def windows_dll_report():
    """Diagnose the native librtlsdr situation (Windows only; {} elsewhere).

    Reports which Python is running, where pyrtlsdr/rtlsdr.dll/libusb-1.0.dll
    were found, the *actual* PE architecture of each file, whether
    pyrtlsdrlib (the self-contained prebuilt wheel) is installed, and whether
    rtlsdr.dll itself can be loaded by ctypes — with the exact WinError when
    it cannot. This turns "wrong bitness" from a guess into a fact.
    """
    if not IS_WINDOWS:
        return {}
    import ctypes

    rep = {
        "python_bits": struct.calcsize("P") * 8,
        "python_exe": sys.executable,
        "pyrtlsdr": None,
        "pyrtlsdrlib": None,
        "pyrtlsdrlib_dll": None,
        "searched_dirs": list(_DLL_CANDIDATE_DIRS),
        "rtlsdr_dll": None,
        "rtlsdr_arch": None,
        "libusb_dll": None,
        "libusb_arch": None,
        "load_ok": False,
        "winerror": None,
        "message": None,
    }

    # rtlsdr.dll / libusb-1.0.dll: check our candidate folders + System32
    system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                            "System32")
    for name, key, arch in (("rtlsdr.dll", "rtlsdr_dll", "rtlsdr_arch"),
                            ("librtlsdr.dll", "rtlsdr_dll", "rtlsdr_arch"),
                            ("libusb-1.0.dll", "libusb_dll", "libusb_arch")):
        if rep[key]:
            continue
        for d in _DLL_CANDIDATE_DIRS + (system32,):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                rep[key] = p
                rep[arch] = _pe_machine(p)
                break

    try:
        import rtlsdr as _rtl
        rep["pyrtlsdr"] = getattr(_rtl, "__version__", "installed")
    except Exception as exc:  # not installed, or its own load failed
        rep["pyrtlsdr"] = f"not usable ({exc})"

    # pyrtlsdrlib: the prebuilt-dll wheel (self-contained x64 librtlsdr)
    try:
        from pyrtlsdrlib.lib import get_library_files
        files = get_library_files()
        rep["pyrtlsdrlib"] = "installed"
        rep["pyrtlsdrlib_dll"] = str(files[0]) if files else None
    except ImportError:
        rep["pyrtlsdrlib"] = "not installed"

    # Preflight: load rtlsdr.dll by absolute path and capture the real error
    if rep["rtlsdr_dll"]:
        try:
            ctypes.WinDLL(rep["rtlsdr_dll"])
            rep["load_ok"] = True
        except OSError as exc:
            rep["winerror"] = getattr(exc, "winerror", None)
            rep["message"] = str(exc)
    return rep


def _import_error_hint():
    if not IS_WINDOWS:
        return (
            "pyrtlsdr is installed but librtlsdr was not found. "
            "Install it with your package manager, e.g. sudo apt install librtlsdr-dev rtl-sdr"
        )

    rep = windows_dll_report()
    bits = rep["python_bits"]
    want = "64-bit (x64)" if bits == 64 else "32-bit (x86)"
    auto_fix = ("run setup.bat (installs the pyrtlsdrlib package with the "
                "correct prebuilt DLL), or pip install pyrtlsdrlib")

    if rep["pyrtlsdrlib"] == "installed":
        # its loader runs before any manual search, so a failure here is odd
        return (
            f"pyrtlsdrlib is installed but its librtlsdr ({want}) failed to "
            f"load. Reinstall it: pip install --force-reinstall pyrtlsdrlib"
        )

    if rep["rtlsdr_dll"] is None:
        return (
            f"pyrtlsdr is installed but rtlsdr.dll was not found. {auto_fix[0].upper()}"
            f"{auto_fix[1:]} — or copy rtlsdr.dll + libusb-1.0.dll ({want}) "
            f"into the app folder or its dlls\\ subfolder (README.md, step 3)."
        )
    if rep["winerror"] == 193:
        arch = rep.get("rtlsdr_arch") or "the wrong bitness"
        other = ""
        if rep.get("libusb_arch") and want not in rep["libusb_arch"]:
            other = (f" libusb-1.0.dll is also {rep['libusb_arch']} — "
                     f"replace both.")
        return (
            f"{rep['rtlsdr_dll']} is {arch} but your Python is {bits}-bit "
            f"(WinError 193).{other} Fix: {auto_fix} — or delete the "
            f"wrong DLL(s) from the app folder; a verified {want} copy "
            f"ships in the app's dlls\\ subfolder."
        )
    if rep["winerror"] == 126:
        return (
            f"rtlsdr.dll found at {rep['rtlsdr_dll']} but a dependency is "
            f"missing (WinError 126) — usually libusb-1.0.dll is absent or "
            f"the wrong bitness. Fix: {auto_fix}, or put the {want} "
            f"libusb-1.0.dll (from the app's dlls\\ subfolder) next to "
            f"rtlsdr.dll."
        )
    if rep["load_ok"]:
        return (
            f"rtlsdr.dll at {rep['rtlsdr_dll']} loads standalone but pyrtlsdr "
            f"still failed ({rep['message']}). Try: pip install --force-reinstall pyrtlsdr"
        )
    return (
        f"pyrtlsdr failed to load librtlsdr ({rep['message'] or 'unknown error'}). "
        f"Run check_dlls.py in the app folder for a full diagnosis."
    )


def open_device(prefer_sim=False):
    """Returns (device, info). Never raises when sim_fallback is allowed.

    Import errors (pyrtlsdr / native DLLs missing) and device-open errors
    (no dongle, driver not bound) are diagnosed separately so the user
    gets the right fix in the status bar.
    """
    from . import config as cfg

    if not prefer_sim:
        try:
            from rtlsdr import RtlSdr  # deferred import: friendly errors
        except (ImportError, OSError):
            hint = _import_error_hint()
        else:
            try:
                dev = RtlSdrDevice(0)
                info = {
                    "mode": "hardware",
                    "device": dev.name,
                    "backend": "pyrtlsdr / librtlsdr",
                    "note": "RTL2832U detected and streaming.",
                }
                return dev, info
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
    if not IS_WINDOWS:
        # Linux: the sysfs probe knows far more than the exception string.
        probe = probe_linux_usb()
        if probe.get("hint"):
            if "looks healthy" not in probe["hint"]:
                return probe["hint"]
            return (
                "Could not open the RTL2832U: %s. %s"
                % (msg, probe["hint"]))
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
