"""Unit tests for the v1.7.2 Linux USB probe (backend/device.py).

Builds fake sysfs/devfs trees in a temp dir and drives
probe_linux_usb() through every diagnostic branch:
  no dongle on the bus / DVB driver bound / module loaded (unbound) /
  devnode missing (Docker passthrough) / devnode not writable (udev) /
  everything healthy / non-RTL devices ignored.
"""
import importlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import device as devmod  # noqa: E402

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail else ""))


def make_tree(fx, vid="0bda", pid="2838", bound_driver=None,
              dvb_module=False, devnode=True, devnode_mode=0o666,
              busnum="3", devnum="5", with_device=True):
    """Fake sysfs + devbus tree. Returns (sys_root, mod_root, dev_root)."""
    sysd = os.path.join(fx, "sys", "bus", "usb", "devices")
    mods = os.path.join(fx, "sys", "module")
    devb = os.path.join(fx, "dev", "bus", "usb")
    os.makedirs(sysd)
    if not with_device:
        return sysd, mods, devb
    dev = os.path.join(sysd, "3-2")
    os.makedirs(dev)
    with open(os.path.join(dev, "idVendor"), "w") as f:
        f.write(vid + "\n")
    with open(os.path.join(dev, "idProduct"), "w") as f:
        f.write(pid + "\n")
    with open(os.path.join(dev, "busnum"), "w") as f:
        f.write(busnum + "\n")
    with open(os.path.join(dev, "devnum"), "w") as f:
        f.write(devnum + "\n")
    # a hub + an unrelated device must be ignored
    os.makedirs(os.path.join(sysd, "usb1"))
    with open(os.path.join(sysd, "usb1", "idVendor"), "w") as f:
        f.write("1d6b\n")
    other = os.path.join(sysd, "3-1")
    os.makedirs(other)
    with open(os.path.join(other, "idVendor"), "w") as f:
        f.write("046d\n")
    with open(os.path.join(other, "idProduct"), "w") as f:
        f.write("c52b\n")
    # interface dir (with optional DVB driver symlink). A colon is legal in
    # Linux sysfs names but NOT in Windows directory names - skip there.
    ifd = os.path.join(sysd, "3-2:1.0")
    if os.name == "posix":
        os.makedirs(ifd)
        if bound_driver and symlinks_ok():
            drvdir = os.path.join(fx, "drivers", bound_driver)
            os.makedirs(drvdir)
            os.symlink(drvdir, os.path.join(ifd, "driver"))
    if dvb_module:
        os.makedirs(os.path.join(mods, "dvb_usb_rtl28xxu"))
    if devnode:
        node = os.path.join(devb, "%03d" % int(busnum), "%03d" % int(devnum))
        os.makedirs(os.path.dirname(node), exist_ok=True)
        with open(node, "w") as f:
            f.write("node")
        os.chmod(node, devnode_mode)
    return sysd, mods, devb


def probe(fx, **kw):
    sysd, mods, devb = make_tree(fx, **kw)
    return devmod.probe_linux_usb(sys_root=sysd, mod_root=mods, dev_root=devb)


_symlink_cache = None


def symlinks_ok():
    """os.symlink needs privileges on Windows - probe once, then gate."""
    global _symlink_cache
    if _symlink_cache is None:
        if os.name != "posix":
            _symlink_cache = False
        else:
            try:
                t = tempfile.mkdtemp()
                a, b = os.path.join(t, "a"), os.path.join(t, "b")
                os.makedirs(a)
                os.symlink(a, b)
                _symlink_cache = os.path.isdir(b)
            except (OSError, NotImplementedError):
                _symlink_cache = False
    return _symlink_cache


def main():
    print("== device probe: fake sysfs trees ==")

    rep = probe(tempfile.mkdtemp(), with_device=False)
    check("no dongle -> present False", rep["present"] is False)
    check("no dongle -> replug hint",
          rep["hint"] and "No RTL2832U" in rep["hint"])

    fx = tempfile.mkdtemp()
    rep = probe(fx, vid="046d", pid="c52b")
    check("non-RTL VID/PID ignored", rep["present"] is False)

    fx = tempfile.mkdtemp()
    rep = probe(fx, bound_driver="dvb_usb_rtl28xxu")
    if symlinks_ok():
        check("DVB bound -> driver_bound set",
              rep["driver_bound"] == "dvb_usb_rtl28xxu", rep["driver_bound"])
        check("DVB bound -> modprobe hint",
              rep["hint"] and "modprobe -r" in rep["hint"]
              and "claimed" in rep["hint"])
    else:
        CHECKS.append(True)   # Windows without symlink privilege: skipped
        print("  (DVB-bound branch skipped: no symlink privilege)")

    fx = tempfile.mkdtemp()
    rep = probe(fx, dvb_module=True)
    check("module loaded, unbound -> warning hint",
          rep["dvb_module_loaded"] and rep["driver_bound"] is None
          and rep["hint"] and "linux-setup.sh" in rep["hint"])

    fx = tempfile.mkdtemp()
    rep = probe(fx, devnode=False)
    check("no devnode -> Docker passthrough hint",
          rep["devnode_ok"] is False and rep["hint"]
          and "--device=/dev/bus/usb" in rep["hint"])
    check("devnode path derived from busnum/devnum",
          rep["devnode"].endswith(os.path.join("003", "005")))

    fx = tempfile.mkdtemp()
    rep = probe(fx, devnode=True, devnode_mode=0o666)
    if os.name == "posix" and os.geteuid() != 0:
        check("writable node -> healthy hint",
              rep["devnode_ok"] is True and "looks healthy" in rep["hint"])
    else:
        # cannot simulate a permission denial as root / on Windows:
        # accept either outcome, just require a verdict
        check("devnode present -> verdict returned",
              rep["devnode_ok"] is not None and rep["hint"])
        print("  (permission branch skipped: root or non-POSIX)")

    if os.name == "posix" and os.geteuid() != 0:
        fx = tempfile.mkdtemp()
        rep = probe(fx, devnode=True, devnode_mode=0o444)
        check("unwritable node -> udev hint",
              rep["devnode_ok"] is False and rep["hint"]
              and "udev" in rep["hint"] and "linux-setup.sh" in rep["hint"])
    else:
        print("  (unwritable-node case skipped: root or non-POSIX)")

    # never raises on a missing tree; on Linux it still produces a verdict
    # for the UI, while off-Linux the probe intentionally no-ops
    tmp = tempfile.mkdtemp()
    rep = devmod.probe_linux_usb(
        sys_root=os.path.join(tmp, "missing"), mod_root=tmp, dev_root=tmp)
    if devmod.IS_LINUX:
        check("missing sysfs -> clean report with verdict",
              rep["present"] is False and rep["hint"]
              and "probe unavailable" in rep["hint"])
    else:
        CHECKS.append(True)
        print("  (missing-sysfs verdict skipped: probe no-ops off-Linux)")

    # ---- v1.7.4: device call serialization --------------------------------
    # librtlsdr is not thread-safe; every RtlSdrDevice entry point must run
    # on one reentrant lock. Drive a fake .sdr from two threads and verify
    # a streaming read excludes concurrent control calls.
    import threading
    import time as _time

    import numpy as np

    dev = devmod.RtlSdrDevice.__new__(devmod.RtlSdrDevice)  # no hw, no pyrtlsdr
    dev._dev_lock = threading.RLock()
    dev._open = True
    dev.index = 0

    events = []

    class FakeSdr:
        def read_samples(self, n):
            events.append("read+")
            _time.sleep(0.02)
            events.append("read-")
            return np.zeros(n, dtype=np.complex64)

        def get_gains(self):
            events.append("gains+")
            _time.sleep(0.005)
            events.append("gains-")
            return [0]

    dev.sdr = FakeSdr()

    def _reader():
        for _ in range(8):
            dev.read_iq(1024)

    def _poller():
        for _ in range(8):
            dev.get_gains()

    ts = [threading.Thread(target=_reader), threading.Thread(target=_poller)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    depth = 0
    interleaved = 0
    for ev in events:
        depth += 1 if ev.endswith("+") else -1
        if depth > 1:
            interleaved += 1
    check("device calls never interleave across threads", interleaved == 0,
          f"{len(events)} events")
    try:
        with dev._dev_lock:
            dev.get_gains()        # same-thread reentry must not deadlock
        check("lock is reentrant (nested same-thread call)", True)
    except Exception as exc:
        check("lock is reentrant (nested same-thread call)", False, str(exc))
    check("read_iq still returns the block", len(dev.read_iq(64)) == 64)

    # ---- v1.7.5: prefer the distro librtlsdr over the wheel's bundled one --
    # find_system_librtlsdr() must locate a distro .so in a given dir and
    # return None when there is none or when the candidate fails the ABI
    # check; prefer_system_librtlsdr() must neutralize pyrtlsdrlib only on
    # Linux and honor the escape hatch.
    d = tempfile.mkdtemp()
    fake = os.path.join(d, "librtlsdr.so.0")
    open(fake, "w").close()
    ok = lambda p: True          # fake files: pretend the ABI is fine
    hit = devmod.find_system_librtlsdr(search_dirs=[d], validate=ok)
    check("find_system_librtlsdr finds a distro lib in a given dir",
          hit == fake, str(hit))
    hit2 = devmod.find_system_librtlsdr(
        search_dirs=[os.path.join(d, "nope")], validate=ok)
    check("find_system_librtlsdr returns None when nothing matches",
          hit2 is None)
    other = os.path.join(d, "librtlsdr.so.0.6.0")
    open(other, "w").close()
    hit3 = devmod.find_system_librtlsdr(search_dirs=[d], validate=ok)
    check("multiple candidates -> deterministic pick",
          hit3 in (fake, other), str(hit3))
    hit4 = devmod.find_system_librtlsdr(search_dirs=[d], validate=lambda p: False)
    check("candidate failing the ABI check is rejected", hit4 is None)
    hit5 = devmod.find_system_librtlsdr(search_dirs=[d])
    check("real ABI validation rejects fake text files", hit5 is None)

    saved = sys.modules.get("pyrtlsdrlib", "absent")
    orig_finder = devmod.find_system_librtlsdr
    import ctypes.util as _ctutil
    saved_find = _ctutil.find_library
    try:
        devmod.find_system_librtlsdr = (
            lambda search_dirs=None, rejected=None: fake)
        rep = devmod.prefer_system_librtlsdr(platform_name="linux",
                                             force_bundled=True)
        check("RTLSDR_PLAYER_FORCE_BUNDLED escape hatch keeps the wheel",
              rep["bundled_disabled"] is False)
        rep2 = devmod.prefer_system_librtlsdr(platform_name="linux")
        _entry = sys.modules.get("pyrtlsdrlib")
        check("linux + distro lib -> bundled wheel disabled",
              rep2["bundled_disabled"] is True
              and (_entry is None
                   or getattr(_entry, "rtlsdr_player_stub", False)))
        rep3 = devmod.prefer_system_librtlsdr(platform_name="win32")
        check("windows -> no-op (bundled dll logic untouched)",
              rep3["bundled_disabled"] is False
              and rep3["system"] is None)
    finally:
        devmod.find_system_librtlsdr = orig_finder
        _ctutil.find_library = saved_find
        if saved == "absent":
            sys.modules.pop("pyrtlsdrlib", None)
        else:
            sys.modules["pyrtlsdrlib"] = saved

    # -- v1.7.9: hard refusal of the bundled fallback -----------------------
    # With no distro lib on Linux, prefer_system_librtlsdr() must record
    # BUNDLED_FALLBACK (the escape hatch and off-Linux platforms must leave
    # it False), the guard message must name the fix, and RtlSdrDevice must
    # raise BundledLibError instead of opening the dongle.
    saved_flag = devmod.BUNDLED_FALLBACK
    saved_finder = devmod.find_system_librtlsdr
    try:
        devmod.find_system_librtlsdr = (
            lambda search_dirs=None, rejected=None: None)
        devmod.BUNDLED_FALLBACK = False   # pretend a system lib was present
        devmod.prefer_system_librtlsdr(platform_name="linux",
                                       force_bundled=True)
        check("escape hatch -> BUNDLED_FALLBACK stays False",
              devmod.BUNDLED_FALLBACK is False)
        devmod.prefer_system_librtlsdr(platform_name="linux")
        check("linux + no distro lib -> BUNDLED_FALLBACK recorded",
              devmod.BUNDLED_FALLBACK is True)
        devmod.prefer_system_librtlsdr(platform_name="win32")
        check("off-linux call leaves the flag alone",
              devmod.BUNDLED_FALLBACK is True)
        gmsg = devmod.bundled_fallback_message()
        check("guard message names apt fix + escape hatch",
              "librtlsdr0" in gmsg and "linux-setup.sh" in gmsg
              and "RTLSDR_PLAYER_FORCE_BUNDLED" in gmsg)
        try:
            importlib.import_module("rtlsdr")
            have_rtlsdr = True
        except Exception:
            have_rtlsdr = False
        if have_rtlsdr and sys.platform.startswith("linux"):
            try:
                devmod.RtlSdrDevice(0)
                raised = "no exception"
            except devmod.BundledLibError as exc:
                raised = "librtlsdr0" in str(exc)
            except Exception as exc:
                raised = "wrong exc: %r" % (exc,)
            check("__init__ refuses to open with the bundled build",
                  raised is True, str(raised))
    finally:
        devmod.find_system_librtlsdr = saved_finder
        devmod.BUNDLED_FALLBACK = saved_flag

    # -- v1.7.8: native library identity (sys.modules + /proc/self/maps) ----
    lst = devmod._mapped_paths("definitely-not-mapped-anywhere-xyz")
    check("_mapped_paths returns [] for absent needle", lst == [])
    if sys.platform.startswith("linux"):
        # /proc/self/maps only exists on Linux (this is its whole point)
        lst2 = devmod._mapped_paths("python")
        check("_mapped_paths finds mapped objects",
              isinstance(lst2, list) and len(lst2) > 0
              and all(p.endswith(".so") or "/" in p for p in lst2),
              str(lst2[:1]))
    ident_path, ident_usb = devmod._native_lib_identity()
    check("_native_lib_identity never raises, str+list",
          isinstance(ident_path, str) and isinstance(ident_usb, list),
          ident_path)
    if sys.platform.startswith("linux"):
        # with pyrtlsdr importable, the identity must be a real .so path
        # (bundled wheel here), never the old 'unknown' regression
        try:
            importlib.import_module("rtlsdr")
            have_rtlsdr = True
        except Exception:
            have_rtlsdr = False
        if have_rtlsdr:
            # bundled wheels name it librtlsdr.so OR librtlsdr.so.<ver>
            # (e.g. .so.0.9git) - accept any versioned soname, but never
            # the old 'unknown' regression
            check("_native_lib_identity resolves the real native lib",
                  ident_path != "unknown"
                  and ".so" in os.path.basename(ident_path),
                  ident_path)

    # -- v1.7.10: dual-generation systems (.so.0 next to .so.2) -------------
    # The validator must report ABI-rejected builds; the success path must
    # stub the pyrtlsdrlib slot with the validated handle and pin
    # find_library('rtlsdr') to that binary.
    import ctypes.util

    d = tempfile.mkdtemp()
    good = os.path.join(d, "librtlsdr.so.0")
    bad = os.path.join(d, "librtlsdr.so.2")
    open(good, "w").close()
    open(bad, "w").close()
    rej = []
    # model petro: find_library hands back the 2.x soname FIRST (ldconfig
    # lists it first there), then the glob adds both sonames
    saved_fl = ctypes.util.find_library
    try:
        ctypes.util.find_library = (
            lambda name: bad if name in ("rtlsdr", "librtlsdr") else None)
        hit = devmod.find_system_librtlsdr(
            search_dirs=[d],
            validate=lambda p: p.endswith(".so.0"),
            rejected=rej)
    finally:
        ctypes.util.find_library = saved_fl
    check("dual-generation dir -> validated .so.0 wins", hit == good, str(hit))
    check("ABI-rejected .so.2 is reported", rej == [bad], str(rej))

    saved_mod10 = sys.modules.get("pyrtlsdrlib", "absent")
    saved_find10 = ctypes.util.find_library
    saved_flag10 = devmod.BUNDLED_FALLBACK
    saved_finder10 = devmod.find_system_librtlsdr
    try:
        # gate on rtlsdr importability BEFORE the stub is installed
        try:
            importlib.import_module("rtlsdr")
            have_rtlsdr = True
        except Exception:
            have_rtlsdr = False
        # a real, loadable .so: libm exists on every Linux (the stubbed
        # finder bypasses the ABI validate)
        real_so = ctypes.util.find_library("m") or None
        if real_so:
            devmod.find_system_librtlsdr = (
                lambda search_dirs=None, rejected=None: real_so)
            devmod.BUNDLED_FALLBACK = False
            rep10 = devmod.prefer_system_librtlsdr(platform_name="linux")
            stub = sys.modules.get("pyrtlsdrlib")
            check("success path installs a stub wheel module",
                  rep10["bundled_disabled"] is True
                  and getattr(stub, "rtlsdr_player_stub", False) is True)
            if getattr(stub, "rtlsdr_player_stub", False):
                h = stub.load_librtlsdr()
                check("stub hands pyrtlsdr a live CDLL handle",
                      h is not None and getattr(h, "_name", "") == real_so,
                      str(h))
            check("find_library('rtlsdr') pinned to the validated lib",
                  ctypes.util.find_library("rtlsdr") == real_so,
                  ctypes.util.find_library("rtlsdr"))
            check("find_library('librtlsdr') pinned too",
                  ctypes.util.find_library("librtlsdr") == real_so)
            check("unrelated find_library queries pass through",
                  ctypes.util.find_library("definitely-not-a-lib-xyz")
                  is None)
            if have_rtlsdr and sys.platform.startswith("linux"):
                # purge and re-import: pyrtlsdr must consult the stub and
                # try to bind libm -> AttributeError at symbol binding.
                # (ImportError instead would mean the stub was ignored and
                # pyrtlsdr's own chain ran out - the pre-v1.7.10 behavior.)
                for _m in [k for k in list(sys.modules)
                           if k == "rtlsdr" or k.startswith("rtlsdr.")]:
                    del sys.modules[_m]
                try:
                    importlib.import_module("rtlsdr")
                    raised = "no exception"
                except AttributeError:
                    raised = "attributeerror"
                except ImportError:
                    raised = "importerror"
                except Exception as exc:
                    raised = "other: %r" % (exc,)
                check("pyrtlsdr consults the stub wheel first",
                      raised == "attributeerror", raised)
                for _m in [k for k in list(sys.modules)
                           if k == "rtlsdr" or k.startswith("rtlsdr.")]:
                    del sys.modules[_m]
    finally:
        devmod.find_system_librtlsdr = saved_finder10
        devmod.BUNDLED_FALLBACK = saved_flag10
        ctypes.util.find_library = saved_find10
        if saved_mod10 == "absent":
            sys.modules.pop("pyrtlsdrlib", None)
        else:
            sys.modules["pyrtlsdrlib"] = saved_mod10

    # -- v1.7.11: the probe must be self-evidencing -------------------------
    # petro printed "no system librtlsdr found" while ldconfig clearly listed
    # a loadable .so.0 - a stale detector next to a fresh config.py. The
    # probe must (a) query the linker cache itself, (b) record every
    # verdict, (c) name the offending symbol in ABI rejections, and (d)
    # carry a version constant the banner can pair against config.VERSION.
    cachetext = "\n".join([
        "604 libusb-1.0.so.0 (libc6,x86-64) => /lib/x86_64-linux-gnu/libusb-1.0.so.0",
        "1234 librtlsdr.so.2 (libc6,x86-64) => /lib/x86_64-linux-gnu/librtlsdr.so.2",
        "1235 librtlsdr.so.0 (libc6,x86-64) => /lib/x86_64-linux-gnu/librtlsdr.so.0",
        "   \tlibusb-1.0.so.0 (libc6) => /lib/i386-linux-gnu/libusb-1.0.so.0",
    ])
    cc = devmod._ldconfig_candidates(cachetext)
    check("ldconfig parser picks both generations in cache order",
          cc == ["/lib/x86_64-linux-gnu/librtlsdr.so.2",
                 "/lib/x86_64-linux-gnu/librtlsdr.so.0"], str(cc))
    check("ldconfig parser ignores unrelated entries and blanks",
          all("librtlsdr" in p for p in cc) and len(cc) == 2, str(cc))
    check("ldconfig parser survives garbage input",
          devmod._ldconfig_candidates("not a cache at all\n===") == [])
    check("DEVICE_LAYER_VERSION declared",
          str(getattr(devmod, "DEVICE_LAYER_VERSION", "")).count(".") == 2,
          str(getattr(devmod, "DEVICE_LAYER_VERSION", "")))

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "empty"), exist_ok=True)
    hitc = devmod.find_system_librtlsdr(
        search_dirs=[os.path.join(d, "empty")],
        validate=lambda p: True,
        ldconfig_text=cachetext)
    check("linker-cache path becomes a candidate outside scan dirs",
          hitc == "/lib/x86_64-linux-gnu/librtlsdr.so.2", str(hitc))
    pv = dict(devmod._LAST_PROBE["verdicts"])
    check("probe table records the linker-cache verdict",
          pv.get(hitc) == "OK - selected", str(pv))

    check("ABI reason for a missing file names CDLL",
          "CDLL failed" in (devmod._abi_reject_reason(
              os.path.join(d, "does-not-exist.so.0")) or ""))
    textso = os.path.join(d, "librtlsdr-text.so.0")
    open(textso, "w").close()
    check("ABI reason for a text file names CDLL",
          "CDLL failed" in (devmod._abi_reject_reason(textso) or ""))

    gcc = shutil.which("gcc") or shutil.which("cc")
    if gcc:
        d2 = tempfile.mkdtemp()
        src0 = os.path.join(d2, "g0.c")
        src2 = os.path.join(d2, "g2.c")
        with open(src0, "w") as f:
            f.write("void *rtlsdr_open(int i){return 0;}\n"
                    "int rtlsdr_read_sync(void*a,char*b,int c,int*d)"
                    "{return 0;}\n"
                    "void rtlsdr_set_dithering(void*d,int o){}\n")
        with open(src2, "w") as f:
            f.write("void *rtlsdr_open(int i){return 0;}\n"
                    "int rtlsdr_read_sync(void*a,char*b,int c,int*d)"
                    "{return 0;}\n")
        import subprocess as _sp
        lib0 = os.path.join(d2, "librtlsdr.so.0")
        lib2 = os.path.join(d2, "librtlsdr.so.2")
        c0 = _sp.run([gcc, "-shared", "-fPIC", "-o", lib0, src0],
                     capture_output=True, text=True)
        c2 = _sp.run([gcc, "-shared", "-fPIC", "-o", lib2, src2],
                     capture_output=True, text=True)
        if c0.returncode == 0 and c2.returncode == 0:
            check("real 0.x build passes the ABI check",
                  devmod._librtlsdr_abi_ok(lib0))
            r2 = devmod._abi_reject_reason(lib2) or ""
            check("real 2.x build rejected, reason names set_dithering",
                  "rtlsdr_set_dithering" in r2, r2)
            rejj = []
            hitr = devmod.find_system_librtlsdr(
                search_dirs=[d2], rejected=rejj, ldconfig_text="")
            check("real dual-generation dir -> 0.x picked by the "
                  "default validator", hitr == lib0, str(hitr))
            d3 = tempfile.mkdtemp()
            lib2only = os.path.join(d3, "librtlsdr.so.2")
            _sp.run([gcc, "-shared", "-fPIC", "-o", lib2only, src2],
                    capture_output=True, text=True)
            rejj3 = []
            hitr3 = devmod.find_system_librtlsdr(
                search_dirs=[d3], rejected=rejj3, ldconfig_text="")
            check("real 2.x-only dir -> None and reported in rejected",
                  hitr3 is None and rejj3 == [lib2only], str(rejj3))
            v3 = dict(devmod._LAST_PROBE["verdicts"])
            check("verdict table names the 2.x rejection reason",
                  "rtlsdr_set_dithering" in (v3.get(lib2only) or ""),
                  str(v3.get(lib2only)))
            check("probe table records scanned dirs",
                  devmod._LAST_PROBE["dirs"] == [d3],
                  str(devmod._LAST_PROBE["dirs"]))
        else:
            check("gcc build of fake libs failed (environment)", False,
                  (c0.stderr + c2.stderr)[:120])
    else:
        check("gcc unavailable - real-.so checks skipped", True)

    print()
    if not all(CHECKS):
        print("DEVICE PROBE TEST: FAIL (%d/%d)"
              % (sum(CHECKS), len(CHECKS)))
        return 1
    print("DEVICE PROBE TEST: ALL PASS (%d)" % len(CHECKS))
    return 0


if __name__ == "__main__":
    shutil.rmtree = shutil.rmtree  # keep flake quiet
    sys.exit(main())
