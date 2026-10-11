#!/usr/bin/env python3
r"""RTL-SDR Player - native DLL diagnostic (Windows).

Run this in the app folder whenever the status bar says the librtlsdr DLLs
were not found, or before asking for help:

    python check_dlls.py

It answers the three questions that decide whether the dongle can open:
  1. Is your Python 32- or 64-bit?         (the DLLs must match)
  2. Are rtlsdr.dll / libusb-1.0.dll present in the app folder (or dlls\)?
  3. Can Windows actually LOAD rtlsdr.dll? (bitness / dependency check)

Exit code 0 = ready for the dongle, 1 = something needs fixing.
"""
import ctypes
import os
import struct
import sys

IS_WINDOWS = sys.platform == "win32"

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATE_DIRS = (
    HERE,
    os.path.join(HERE, "dlls"),
    os.path.join(HERE, "drivers"),
)

PE_MACHINE = {0x014C: "32-bit (x86)", 0x8664: "64-bit (x64)",
              0xAA64: "64-bit (ARM64)"}


def dll_machine_type(path):
    """Read the PE header: is this DLL 32- or 64-bit?"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
        if len(data) < 0x40 or data[:2] != b"MZ":
            return None
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_off + 6 > len(data):
            return None
        machine = struct.unpack_from("<H", data, pe_off + 4)[0]
        return PE_MACHINE.get(machine, hex(machine))
    except OSError:
        return None


def find(name):
    for d in CANDIDATE_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # The system-wide places Windows searches anyway
    system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                            "System32")
    p = os.path.join(system32, name)
    return p if os.path.isfile(p) else None


def main():
    print()
    print("  RTL-SDR Player - DLL diagnostic")
    print("  " + "-" * 46)

    if not IS_WINDOWS:
        print("  This tool is for Windows. On Linux/macOS librtlsdr comes")
        print("  from the package manager (e.g. apt install librtlsdr-dev).")
        return 0 if _linux_ok() else 1

    ok = True

    # 1. Python bitness -------------------------------------------------
    bits = struct.calcsize("P") * 8
    print(f"  Python          : {bits}-bit  ({sys.executable})")

    # 2. pyrtlsdr --------------------------------------------------------
    try:
        import rtlsdr as _rtl
        ver = getattr(_rtl, "__version__", "?")
        print(f"  pyrtlsdr        : installed (v{ver})")
    except Exception as exc:
        print(f"  pyrtlsdr        : NOT usable -> {exc}")
        print("                    fix: pip install --force-reinstall pyrtlsdr")
        return 1

    # 3. the two DLL files ------------------------------------------------
    rtlsdr = find("rtlsdr.dll") or find("librtlsdr.dll")
    libusb = find("libusb-1.0.dll")
    print(f"  rtlsdr.dll      : {rtlsdr or 'NOT FOUND'}")
    print(f"  libusb-1.0.dll  : {libusb or 'NOT FOUND'}")

    if rtlsdr is None or libusb is None:
        ok = False
        print()
        print("  [X] Missing DLL(s). Fix:")
        print("      1. Download the librtlsdr 'win32-x64' release zip:")
        print("         https://github.com/librtlsdr/librtlsdr/releases")
        print("         (or the RTL-SDR Blog driver zip, which bundles them)")
        print("      2. Copy rtlsdr.dll and libusb-1.0.dll into:")
        print(f"         {HERE}")
        print("         (the dlls\\ subfolder also works)")
        if bits == 64:
            print("      3. They MUST be the 64-bit (x64) DLLs - 32-bit ones")
            print("         fail with WinError 193.")
        return 1

    # 4. bitness match -------------------------------------------------
    mt = dll_machine_type(rtlsdr)
    print(f"  rtlsdr.dll arch : {mt or 'unknown'}")
    mtu = dll_machine_type(libusb)
    print(f"  libusb arch     : {mtu or 'unknown'}")
    want = "64-bit (x64)" if bits == 64 else "32-bit (x86)"
    for label, path, kind in (("rtlsdr.dll", rtlsdr, mt),
                              ("libusb-1.0.dll", libusb, mtu)):
        if kind and want not in kind:
            ok = False
            print(f"  [X] {label} is {kind} but your Python is {bits}-bit.")
            print(f"      Replace it with the {want} build (WinError 193 cause).")

    # 5. can Windows actually load it? -----------------------------------
    try:
        ctypes.WinDLL(rtlsdr)
        print("  Load test       : rtlsdr.dll loads OK")
    except OSError as exc:
        ok = False
        print(f"  Load test       : FAILED (WinError {exc.winerror})")
        print(f"                    {exc}")
        if exc.winerror == 126:
            print("      Usually libusb-1.0.dll missing next to rtlsdr.dll,")
            print("      or itself the wrong bitness / broken download.")
        if exc.winerror == 193:
            print("      Bitness mismatch - see above.")

    print("  " + "-" * 46)
    if ok:
        print("  [ok] DLLs look good. If the app still falls back to the")
        print("       simulator, the next cause is the USB driver: run")
        print("       Zadig and bind WinUSB to 'Bulk-In, Interface 0'")
        print("       (README.md, step 2), then click 'Use dongle'.")
    else:
        print("  [X] Fix the items marked [X] above, then re-run:")
        print(f"      python {os.path.basename(sys.argv[0])}")
    print()
    return 0 if ok else 1


def _linux_ok():
    try:
        ctypes.CDLL("librtlsdr.so")
        print("  librtlsdr.so loads OK")
        return True
    except OSError as exc:
        print(f"  librtlsdr.so failed to load: {exc}")
        print("  fix: sudo apt install librtlsdr-dev rtl-sdr")
        return False


if __name__ == "__main__":
    sys.exit(main())
