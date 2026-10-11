#!/usr/bin/env python3
r"""RTL-SDR Player - native DLL diagnostic (Windows).

Run this in the app folder whenever the status bar says the librtlsdr DLLs
were not found, or before asking for help:

    python check_dlls.py

It answers the four questions that decide whether the dongle can open:
  1. Is your Python 32- or 64-bit?          (the DLLs must match)
  2. Is pyrtlsdrlib installed?              (ships the correct DLL itself)
  3. Are rtlsdr.dll / libusb-1.0.dll found, and what bitness are they REALLY?
  4. Can Windows actually LOAD rtlsdr.dll?  (dependency check)

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
    want = "64-bit (x64)" if bits == 64 else "32-bit (x86)"
    print(f"  Python          : {bits}-bit  ({sys.executable})")

    # 2. pyrtlsdrlib - the prebuilt-dll wheel -----------------------------
    ptl = None
    try:
        from pyrtlsdrlib.lib import get_library_files
        files = get_library_files()
        ptl = str(files[0]) if files else None
        print(f"  pyrtlsdrlib     : installed -> {ptl}")
        print("                    (pyrtlsdr loads this automatically;")
        print("                     the checks below are just fallback info)")
    except ImportError:
        print("  pyrtlsdrlib     : not installed")
        print(f"                    easiest fix:  pip install pyrtlsdrlib")
        print(f"                    (or just run setup.bat) - it bundles the")
        print(f"                    {want} librtlsdr DLL, no manual files needed")

    # 3. pyrtlsdr --------------------------------------------------------
    try:
        import rtlsdr as _rtl
        ver = getattr(_rtl, "__version__", "?")
        print(f"  pyrtlsdr        : installed (v{ver})")
    except Exception as exc:
        print(f"  pyrtlsdr        : NOT usable -> {exc}")
        print("                    fix: pip install --force-reinstall pyrtlsdr")
        return 1

    # 4. the two DLL files (fallback source) ------------------------------
    rtlsdr = find("rtlsdr.dll") or find("librtlsdr.dll")
    libusb = find("libusb-1.0.dll")
    print(f"  rtlsdr.dll      : {rtlsdr or 'NOT FOUND'}")
    print(f"  libusb-1.0.dll  : {libusb or 'NOT FOUND'}")

    if rtlsdr is not None:
        mt = dll_machine_type(rtlsdr)
        print(f"  rtlsdr.dll arch : {mt or 'unknown/corrupt'}")
        if mt and want not in mt:
            ok = False
            print(f"  [X] rtlsdr.dll is {mt} but Python is {bits}-bit.")
            print(f"      This file is the problem. Delete it from the app")
            print(f"      folder and {('pyrtlsdrlib will take over' if ptl else 'run pip install pyrtlsdrlib')}.")
    if libusb is not None:
        mtu = dll_machine_type(libusb)
        print(f"  libusb arch     : {mtu or 'unknown/corrupt'}")
        if mtu and want not in mtu and not ptl:
            ok = False
            print(f"  [X] libusb-1.0.dll is {mtu} but Python is {bits}-bit.")

    if rtlsdr is None and not ptl:
        ok = False
        print()
        print("  [X] No rtlsdr.dll/librtlsdr.dll anywhere and pyrtlsdrlib is")
        print("      not installed. Fix (pick one):")
        print(f"      1. pip install pyrtlsdrlib        <- easiest")
        print(f"         (or re-run setup.bat; it does this for you)")
        print("      2. Copy the DLLs from the app's dlls\\ subfolder to")
        print(f"         the app folder: {HERE}")
        print("      3. Manual download (x64!):")
        print("         https://github.com/librtlsdr/librtlsdr/releases")

    # 5. can Windows actually load it? -----------------------------------
    if rtlsdr is not None:
        try:
            ctypes.WinDLL(rtlsdr)
            print("  Load test       : rtlsdr.dll loads OK")
        except OSError as exc:
            ok = False
            print(f"  Load test       : FAILED (WinError {exc.winerror})")
            print(f"                    {exc}")
            if exc.winerror == 126 and not ptl:
                print("      libusb-1.0.dll missing next to it, or wrong")
                print("      bitness. A correct copy is in the app's dlls\\")
                print("      subfolder - or just pip install pyrtlsdrlib.")
            if exc.winerror == 193:
                print("      Bitness mismatch - see above.")

    print("  " + "-" * 46)
    if ok:
        if ptl:
            print("  [ok] Ready: pyrtlsdrlib provides the native librtlsdr.")
            print("       If the app still falls back to the simulator, the")
            print("       next cause is the USB driver: run Zadig and bind")
            print("       WinUSB to 'Bulk-In, Interface 0' (README step 2),")
            print("       then click 'Use dongle'.")
        else:
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
        import rtlsdr  # noqa: F401  (loads via pyrtlsdrlib or system lib)
        print("  pyrtlsdr + librtlsdr load OK")
        return True
    except ImportError as exc:
        print(f"  librtlsdr failed to load: {exc}")
        print("  fix: pip install pyrtlsdrlib")
        print("  or:  sudo apt install librtlsdr-dev rtl-sdr")
        return False


if __name__ == "__main__":
    sys.exit(main())
