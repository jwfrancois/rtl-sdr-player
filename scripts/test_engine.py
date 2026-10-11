"""Engine-level integration test: sim device + demods + squelch + scanner."""
import sys
import time

sys.path.insert(0, "/home/z/my-project/download/rtl-sdr-player")

import numpy as np

from backend import config as cfg
from backend import dsp, device as devmod
from backend.stream import ReceiverEngine


def rms_db(pcm):
    r = float(np.sqrt(np.mean(np.asarray(pcm, dtype=np.float64) ** 2))) / 32768.0
    return 20.0 * np.log10(r + 1e-9)


def run_block_band(band, freq, blocks=6):
    conf = cfg.load_config()
    eng = ReceiverEngine(conf, prefer_sim=True)
    eng.device, eng.info = devmod.open_device(prefer_sim=True)
    eng.band, eng.freq = band, freq
    eng._apply_device_config()
    eng._demod = eng._make_demod()
    out = []
    for _ in range(blocks):
        raw = eng.device.read_iq(dsp.BLOCK)
        pcm = eng._demod.process(np.asarray(raw, dtype=np.complex64))
        out.append((rms_db(pcm), eng._demod.last_chan_db))
    return out


def main():
    print("== WFM 96.1 (RDS station) ==")
    for rms, chan in run_block_band("wfm", 96.1e6):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== WX 162.550 (NBFM) ==")
    for rms, chan in run_block_band("wx", 162.55e6):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== WX 162.475 (noise only) ==")
    for rms, chan in run_block_band("wx", 162.475e6):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== AIR 121.5 (AM) ==")
    for rms, chan in run_block_band("air", 121.5e6):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== AIR 126.700 (noise only) ==")
    for rms, chan in run_block_band("air", 126.7e6):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== AM 820 (direct sampling) ==")
    for rms, chan in run_block_band("am", 820e3):
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")

    # ---- full engine: RDS + squelch + scanner over REST-like calls ----
    print("== full engine: RDS on 96.1 ==")
    conf = cfg.load_config()
    eng = ReceiverEngine(conf, prefer_sim=True)
    eng.start()
    try:
        eng.tune(band="wfm", freq_hz=96_100_000)
        deadline = time.time() + 14
        snap = None
        while time.time() < deadline:
            snap = eng.current_rds()
            if snap and snap["ps"] and snap["groups"] > 60:
                break
            time.sleep(0.4)
        print("  rds:", {k: snap[k] for k in
                         ("pi", "ps", "pty", "af_mhz", "groups", "pilot", "ppm")})
        print("  rt:", snap["radiotext"])
        rds_ok = snap["pi"] == "2A5B" and snap["ps"] == "SIM-RDS"

        print("== squelch: open on 162.550, closed on 162.475 ==")
        eng.tune(band="wx", freq_hz=162_550_000)
        eng.set_squelch(-20.0)
        time.sleep(1.2)
        open_state = eng.squelch_open
        chan_open = eng.chan_db
        eng.tune(band="wx", freq_hz=162_475_000)
        time.sleep(1.2)
        closed_state = eng.squelch_open
        chan_closed = eng.chan_db
        print(f"  162.550: chan {chan_open:.1f} dB open={open_state}; "
              f"162.475: chan {chan_closed:.1f} dB open={closed_state}")
        sq_ok = open_state and not closed_state

        print("== scanner: NOAA list finds activity ==")
        eng.start_scan("noaa")
        deadline = time.time() + 25
        found = None
        while time.time() < deadline:
            st = eng.scan_status()
            if st.get("last_active_hz"):
                found = st
                break
            time.sleep(0.3)
        eng.stop_scan()
        print("  scan status:", {k: found[k] for k in
                                 ("list", "state", "freq_hz", "last_active_hz")}
              if found else "nothing found")
        scan_ok = bool(found) and found["last_active_hz"] in cfg.NOAA_CHANNELS

        print("RESULT:", "PASS" if (rds_ok and sq_ok and scan_ok) else
              f"FAIL (rds={rds_ok} squelch={sq_ok} scan={scan_ok})")
        return 0 if (rds_ok and sq_ok and scan_ok) else 1
    finally:
        eng.close()


if __name__ == "__main__":
    sys.exit(main())
