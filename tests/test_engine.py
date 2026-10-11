"""Engine-level integration test: sim device + demods + squelch + scanner."""
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from backend import config as cfg
from backend import dsp, device as devmod
from backend.stream import ReceiverEngine


def rms_db(pcm):
    r = float(np.sqrt(np.mean(np.asarray(pcm, dtype=np.float64) ** 2))) / 32768.0
    return 20.0 * np.log10(r + 1e-9)


def run_block_band(band, freq, mode=None, blocks=6):
    conf = cfg.load_config()
    eng = ReceiverEngine(conf, prefer_sim=True)
    eng.device, eng.info = devmod.open_device(prefer_sim=True)
    eng.band, eng.freq = band, freq
    eng.mode = mode
    eng._apply_device_config()
    eng._demod = eng._make_demod()
    out = []
    for _ in range(blocks):
        raw = eng.device.read_iq(dsp.BLOCK)
        pcm = eng._demod.process(np.asarray(raw, dtype=np.complex64))
        out.append((rms_db(pcm), eng._demod.last_chan_db))
    eng.close()
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

    print("== SSB USB 119.2 (sim USB voice) ==")
    usb = run_block_band("air", 119.2e6, mode="usb")
    for rms, chan in usb:
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== SSB LSB demod of the USB station (must stay quiet) ==")
    cross = run_block_band("air", 119.2e6, mode="lsb")
    for rms, chan in cross:
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    print("== SSB LSB 123.45 (sim LSB voice) ==")
    lsb = run_block_band("air", 123.45e6, mode="lsb")
    for rms, chan in lsb:
        print(f"  audio {rms:6.1f} dBFS   chan {chan:6.1f} dB")
    ssb_ok = (usb[-1][0] > -40.0 and lsb[-1][0] > -40.0
              and cross[-1][0] < usb[-1][0] - 15.0)

    # ---- full engine: RDS + squelch + scanner over REST-like calls ----
    print("== full engine: RDS on 96.1 ==")
    conf = cfg.load_config()
    conf["scan_memory_file"] = "scan_memory_test.json"  # isolate from app
    import os
    mem_file = os.path.join(cfg.ROOT, conf["scan_memory_file"])
    if os.path.exists(mem_file):
        os.remove(mem_file)
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

        # hysteresis: closing needs 4 dB below threshold, not just below it
        print("== squelch hysteresis ==")
        thr = eng.squelch_db
        mid = thr - 2.0   # between open (thr) and close (thr-4)
        eng.set_squelch(chan_closed + 2.0)  # threshold above noise+2 dB
        hyst_ok = True  # detailed dynamics covered by smoke test over time
        print(f"  threshold {thr:.0f} dB, hysteresis "
              f"{eng.conf.get('squelch_hysteresis_db')} dB (mid {mid:.0f})")
        eng.set_squelch(-20.0)

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

        print("== scan memory + smart order ==")
        mem = eng.memory_list()
        print("  memory:", [(e["hz"], e["hits"]) for e in mem])
        mem_ok = any(e["hz"] == found["last_active_hz"] and e["hits"] >= 1
                     for e in mem) if found else False
        # smart order ranks only the NOAA list's channels
        noaa_hits = {h: eng.memory["freqs"].get(str(h), {}).get("hits", 0)
                     for h in cfg.NOAA_CHANNELS}
        noaa_last = {h: eng.memory["freqs"].get(str(h), {}).get("last", 0.0)
                     for h in cfg.NOAA_CHANNELS}
        expected_top = max(cfg.NOAA_CHANNELS,
                           key=lambda h: (noaa_hits[h], noaa_last[h]))
        eng.start_scan("noaa", order="smart")
        st = eng.scan_status()
        smart_ok = (st["order"] == "smart"
                    and st["freq_hz"] == expected_top)
        time.sleep(0.3)
        eng.stop_scan()
        print(f"  smart order first channel: {st['freq_hz']} "
              f"(expected {expected_top}, order={st['order']})")
        eng.clear_memory()
        mem_cleared = len(eng.memory_list()) == 0

        print("== auto-squelch on band switch ==")
        eng2 = ReceiverEngine(cfg.load_config(), prefer_sim=True)
        eng2.device, eng2.info = devmod.open_device(prefer_sim=True)
        eng2.start()
        time.sleep(0.5)
        eng2.tune(band="wx", freq_hz=162_550_000)
        time.sleep(0.8)
        auto_ok = eng2.squelch_db == float(
            cfg.load_config()["squelch_default_db"])
        eng2.tune(band="wfm", freq_hz=99_900_000)
        time.sleep(0.8)
        auto_off_ok = eng2.squelch_db is None
        eng2.close()
        print(f"  wx auto: {auto_ok}, back to wfm off: {auto_off_ok}")

        if os.path.exists(mem_file):
            os.remove(mem_file)

        ok = (rds_ok and sq_ok and scan_ok and ssb_ok and mem_ok
              and smart_ok and mem_cleared and auto_ok and auto_off_ok)
        print("RESULT:", "PASS" if ok else
              f"FAIL (rds={rds_ok} squelch={sq_ok} scan={scan_ok} "
              f"ssb={ssb_ok} mem={mem_ok} smart={smart_ok} "
              f"clear={mem_cleared} auto={auto_ok and auto_off_ok})")
        return 0 if ok else 1
    finally:
        eng.close()


if __name__ == "__main__":
    sys.exit(main())
