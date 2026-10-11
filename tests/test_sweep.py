"""v1.4 engine-level tests: all-band sweep + 20 named station presets.

Runs against the sim device in realtime (0.109 s per block), so dwell
times are kept small and sweeps are segmented to stay fast.
"""
import os
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config as cfg
from backend.stream import ReceiverEngine


def wait_sweep_done(eng, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sc = eng.scan_status()
        if not sc.get("running") and sc.get("mode") == "sweep":
            return sc
        time.sleep(0.15)
    return eng.scan_status()


def main():
    conf = cfg.load_config()
    conf["scan_memory_file"] = "scan_memory_test.json"
    conf["bookmarks_file"] = "bookmarks_test.json"
    conf["stations_file"] = "stations_test.json"
    conf["sweep_results_file"] = "sweep_results_test.json"
    for f in (conf["scan_memory_file"], conf["bookmarks_file"],
              conf["stations_file"], conf["sweep_results_file"]):
        p = os.path.join(cfg.ROOT, f)
        if os.path.exists(p):
            os.remove(p)

    eng = ReceiverEngine(conf, prefer_sim=True)
    eng.start()
    ok = {}
    try:
        # --------------------------------------------------------------
        # 1. scan lists grew: FM broadcast + AM broadcast full sweeps
        # --------------------------------------------------------------
        ok["fm_list"] = len(cfg.SCAN_LISTS["fm_band"]["freqs"]) == 206
        ok["am_list"] = len(cfg.SCAN_LISTS["am_band"]["freqs"]) == 118
        print(f"scan lists: fm={len(cfg.SCAN_LISTS['fm_band']['freqs'])} "
              f"am={len(cfg.SCAN_LISTS['am_band']['freqs'])}")

        # --------------------------------------------------------------
        # 2. segment sweep over the sim RDS station: power + PS name
        # --------------------------------------------------------------
        eng.tune(band="wfm", freq_hz=98_300_000)
        time.sleep(0.5)
        res = eng.start_sweep(bands=["wfm"], from_hz=95_900_000,
                              to_hz=96_300_000, dwell_s=0.2)
        ok["seg_start"] = bool(res["ok"]) and res["channels"] == 5
        sc = wait_sweep_done(eng, 20)
        time.sleep(0.6)          # let the restore tune actually apply
        results = eng.sweep_results_list()
        print("segment sweep:", [(r["band"], r["freq_hz"], r["db"], r["name"])
                                 for r in results])
        r961 = next((r for r in results if r["freq_hz"] == 96_100_000), None)
        ok["seg_found"] = r961 is not None
        ok["rds_named"] = bool(r961 and r961["name"] == "SIM-RDS")
        ok["restored"] = (eng.band == "wfm" and eng.freq == 98_300_000)
        ok["state_done"] = sc.get("state") == "done"
        print(f"  found={ok['seg_found']} name={r961['name'] if r961 else None} "
              f"restored={ok['restored']} state={sc.get('state')}")

        # --------------------------------------------------------------
        # 3. NOAA sweep: default names from the WX table
        # --------------------------------------------------------------
        eng.start_sweep(bands=["wx"], dwell_s=0.18)
        wait_sweep_done(eng, 20)
        wx = {r["freq_hz"]: r["name"] for r in eng.sweep_results_list()}
        print("wx sweep:", wx)
        ok["wx_names"] = (wx.get(162_400_000) == "NOAA WX2"
                          and wx.get(162_550_000) == "NOAA WX1")

        # --------------------------------------------------------------
        # 4. sweep interrupted by a manual tune: partial results kept,
        #    tuning NOT yanked back
        # --------------------------------------------------------------
        eng.start_sweep(bands=["wx"], dwell_s=0.4)
        time.sleep(1.2)
        eng.tune(band="air", freq_hz=121_500_000)
        time.sleep(0.4)
        sc = eng.scan_status()
        ok["manual_preempts"] = not sc.get("running")
        ok["manual_freq"] = (eng.band == "air" and eng.freq == 121_500_000)
        print(f"  running={sc.get('running')} band={eng.band} "
              f"freq={eng.freq} partial={len(eng.sweep_results)}")

        # --------------------------------------------------------------
        # 5. multi-band sweep (wfm segment via monkeypatch + wx), then
        #    fill the 20 station slots from the results
        # --------------------------------------------------------------
        orig = cfg._band_channels
        cfg._band_channels = (lambda band: ([95_900_000, 96_000_000,
                                             96_100_000, 96_200_000,
                                             96_300_000]
                                            if band == "wfm"
                                            else orig(band)))
        try:
            eng.start_sweep(bands=["wfm", "wx"], dwell_s=0.18)
            wait_sweep_done(eng, 25)
        finally:
            cfg._band_channels = orig
        picks = eng.fill_stations_from_sweep()
        slots = eng.stations_list()
        names = [s["name"] for s in slots[:3]]
        print("fill picks:", [(p["band"], p["freq_hz"], p["name"])
                              for p in picks])
        ok["fill_count"] = len(picks) == 3 and len(slots) == 20
        ok["fill_names"] = ("SIM-RDS" in names and "NOAA WX1" in names
                            and "NOAA WX2" in names)
        ok["fill_mode"] = all(s["mode"] in (None, "wfm", "nbfm")
                              for s in slots if s)
        print(f"  slots[:3] names={names}")

        # --------------------------------------------------------------
        # 6. stations CRUD: save current (live RDS name), rename, delete
        # --------------------------------------------------------------
        eng.tune(band="wfm", freq_hz=96_100_000)
        deadline = time.time() + 10
        while time.time() < deadline:
            snap = eng.current_rds()
            if snap and snap.get("ps"):
                break
            time.sleep(0.3)
        st = eng.set_station(20)                   # live RDS should name it
        ok["save_live_name"] = st["name"] == "SIM-RDS"
        st2 = eng.set_station(2, band="wx", freq_hz=162_550_000)
        ok["save_default_name"] = st2["name"] == "NOAA WX1"
        st3 = eng.set_station(2, name="Weather")
        ok["rename"] = (st3["name"] == "Weather"
                        and st3["freq_hz"] == 162_550_000)
        ok["slot_clear"] = eng.clear_station(2) and \
            eng.stations_list()[1] is None
        try:
            eng.set_station(21)
            ok["slot_range"] = False
        except ValueError:
            ok["slot_range"] = True
        try:
            eng.set_station(5, name="X")           # rename an empty slot
            ok["empty_slot"] = False
        except ValueError:
            ok["empty_slot"] = True
        ok["clear_all"] = eng.clear_stations()["ok"] and \
            all(s is None for s in eng.stations_list())
        print(f"  live-name={ok['save_live_name']} default={ok['save_default_name']} "
              f"rename={ok['rename']} range={ok['slot_range']}")

        # --------------------------------------------------------------
        # 7. dedupe: adjacent-channel shoulders merge to one slot
        # --------------------------------------------------------------
        ded = ReceiverEngine._dedupe_results([
            {"band": "wfm", "freq_hz": 96_000_000, "db": -14.0, "name": "x"},
            {"band": "wfm", "freq_hz": 96_100_000, "db": -8.0, "name": "y"},
            {"band": "wfm", "freq_hz": 96_200_000, "db": -20.0, "name": "z"},
            {"band": "am", "freq_hz": 820_000, "db": -30.0, "name": "a"},
        ])
        ok["dedupe"] = [(d["band"], d["freq_hz"]) for d in ded] == \
            [("wfm", 96_100_000), ("am", 820_000)]
        print(f"  dedupe -> {[(d['band'], d['freq_hz']) for d in ded]}")

        # --------------------------------------------------------------
        # 8. persistence: fresh engine reloads stations + sweep results
        # --------------------------------------------------------------
        eng.set_station(1, band="wfm", freq_hz=96_100_000, name="Persist")
        eng2 = ReceiverEngine(conf, prefer_sim=True)
        ok["stations_persist"] = (eng2.stations_list()[0] is not None
                                  and eng2.stations_list()[0]["name"]
                                  == "Persist")
        ok["sweep_persist"] = len(eng2.sweep_results) >= 3
        print(f"  stations reloaded={ok['stations_persist']} "
              f"sweep results reloaded={len(eng2.sweep_results)}")

        # --------------------------------------------------------------
        # 9. live scanner still works on the new full-band lists
        # --------------------------------------------------------------
        eng.start_scan("am_band")
        st = eng.scan_status()
        ok["live_am_list"] = (st["running"] and st["count"] == 118
                              and st["mode"] == "live")
        time.sleep(0.5)
        eng.stop_scan()
        ok["live_stop"] = not eng.scan_status()["running"]

        # fill error path with no results must 400 (ValueError)
        eng.sweep_results = []
        try:
            eng.fill_stations_from_sweep()
            ok["fill_no_results"] = False
        except ValueError:
            ok["fill_no_results"] = True
    finally:
        eng.close()

    passed = sum(1 for v in ok.values() if v)
    total = len(ok)
    for k, v in ok.items():
        if not v:
            print(f"  FAILED: {k}")
    print(f"RESULT: {'PASS' if passed == total else 'FAIL'} "
          f"({passed}/{total})")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
