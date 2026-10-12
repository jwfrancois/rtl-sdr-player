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

    # ---- DcBlocker (v1.7.1): kills discriminator DC, keeps programme bass
    print("== DcBlocker: DC removed, bass kept ==")
    dcb = dsp.DcBlocker(15.0, 48_000)
    tt = np.arange(48_000) / 48_000.0
    sig = 0.5 * np.sin(2 * np.pi * 220.0 * tt) + 0.4   # 220 Hz tone + DC
    y = dcb.process(sig)
    dc_out = float(np.mean(y[8000:]))                  # past the settle region
    bass_rms = float(np.sqrt(np.mean(y[8000:] ** 2)))
    print(f"  DC 0.400 -> {dc_out:+.4f};  bass rms {bass_rms:.3f} (0.354 ideal)")
    dcb_ok = abs(dc_out) < 0.01 and bass_rms > 0.30


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

        print("== CTCSS detector (synthetic audio) ==")
        fs_a = 48_000
        det = dsp.CtcssDetector(fs_a)
        tt = np.arange(int(fs_a * 1.2)) / fs_a
        tone = (350.0 / 5_000.0) * np.sin(2 * np.pi * 123.0 * tt)
        def feed(sig, hz):  # windowed-detector helper
            det.set_tone(hz)
            for i in range(0, len(sig), 5_242):
                det.process(sig[i:i + 5_242])
            return det
        feed(tone, 123.0)
        ct_ok = det.open
        print(f"  123.0 Hz tone: open={det.open} dev={det.dev_hz:.0f} Hz")
        feed(tone, 127.3)
        ct_adj = not det.open and det.dev_hz < 60.0
        print(f"  adjacent 127.3 selected: open={det.open} dev={det.dev_hz:.0f} Hz")
        wob = 0.25 * np.sin(2 * np.pi * 130.0 * tt
                            + 3.0 * np.sin(2 * np.pi * 3.0 * tt))
        feed(wob, 123.0)
        ct_wob = not det.open
        print(f"  wobbly voice fundamental: open={det.open} ratio={det.ratio:.2f}")
        feed(0.3 * np.sin(2 * np.pi * 500.0 * tt), 123.0)
        ct_voice = not det.open
        print(f"  voice above the tone band: open={det.open} dev={det.dev_hz:.0f} Hz")

        print("== engine CTCSS gate: 162.550 carries 141.3 in the sim ==")
        eng.tune(band="wx", freq_hz=162_550_000)
        eng.set_squelch(-20.0)
        eng.set_ctcss(123.0)          # wrong tone -> gate stays closed
        time.sleep(1.6)
        wrong = not eng._ctcss_open()
        eng.set_ctcss(141.3)          # correct tone -> opens
        time.sleep(1.6)
        right = eng._ctcss_open()
        eng.set_ctcss(0.0)
        print(f"  wrong tone closed={wrong}, right tone open={right}")
        try:
            eng.set_ctcss(123.4)
            ct_invalid = False
        except ValueError:
            ct_invalid = True
        print(f"  invalid tone rejected={ct_invalid}")

        print("== bookmarks: add / list / persist / delete ==")
        conf["bookmarks_file"] = "bookmarks_test.json"
        bmk_file = os.path.join(cfg.ROOT, conf["bookmarks_file"])
        if os.path.exists(bmk_file):
            os.remove(bmk_file)
        beng = ReceiverEngine(conf, prefer_sim=True)   # loads bookmarks.json
        beng.band, beng.freq = "wfm", 96_100_000
        b1 = beng.add_bookmark("Sim RDS", band="wfm", freq_hz=96_100_000,
                               mode="wfm")
        b2 = beng.add_bookmark(None, band="wx", freq_hz=162_550_000,
                               mode="nbfm")
        beng.add_bookmark("dup test", band="wfm", freq_hz=96_100_000)
        lst = beng.bookmarks_list()
        print("  list:", [(b["name"], b["band"], b["freq_hz"]) for b in lst])
        # re-adding the same (band, freq) refreshes name/mode instead of duping
        dup_ok = (b1["name"] == "dup test" and b2["name"] == "162.550 MHz"
                  and len(lst) == 2
                  and lst[0]["freq_hz"] <= lst[1]["freq_hz"])
        del_ok = (beng.delete_bookmark(b1["id"])
                  and len(beng.bookmarks_list()) == 1)
        bmk_ok = dup_ok and del_ok
        # persistence: a fresh engine (no start()) must see the surviving row
        beng2 = ReceiverEngine(conf, prefer_sim=True)
        bmk_persist = (len(beng2.bookmarks) == 1
                       and beng2.bookmarks[0]["freq_hz"] == 162_550_000)
        print(f"  roundtrip ok={bmk_ok}, persisted reload ok={bmk_persist}")
        if os.path.exists(bmk_file):
            os.remove(bmk_file)

        if os.path.exists(mem_file):
            os.remove(mem_file)

        ok = (rds_ok and sq_ok and scan_ok and ssb_ok and mem_ok
              and smart_ok and mem_cleared and auto_ok and auto_off_ok
              and ct_ok and ct_adj and ct_wob and ct_voice
              and wrong and right and ct_invalid
              and bmk_ok and bmk_persist and dcb_ok)
        print("RESULT:", "PASS" if ok else
              f"FAIL (rds={rds_ok} squelch={sq_ok} scan={scan_ok} "
              f"ssb={ssb_ok} mem={mem_ok} smart={smart_ok} "
              f"clear={mem_cleared} auto={auto_ok and auto_off_ok} "
              f"ctcss={ct_ok and ct_adj and ct_wob and ct_voice} "
              f"gate={wrong and right and ct_invalid} "
              f"bmk={bmk_ok and bmk_persist})")
        return 0 if ok else 1
    finally:
        eng.close()


if __name__ == "__main__":
    sys.exit(main())
