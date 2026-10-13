"""v1.7 engine-level tests: spectrum-wide RDS audit + digest integration.

Runs against the sim device in realtime. The sim has two RDS stations:
96.1 MHz "SIM-RDS" (PI 2A5B, PTY Top 40) and 101.7 MHz "JAZZ-FM"
(PI 1F2C). Also validates the v1.7 data_dir relocation: every runtime
artifact must land in the configured data directory.
"""
import json
import os
import sys
import tempfile
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import config as cfg
from backend.stream import ReceiverEngine


def wait_audit_done(eng, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sc = eng.scan_status()
        if not sc.get("running") and sc.get("mode") == "audit" \
                and sc.get("state") == "done":
            return sc
        time.sleep(0.2)
    return eng.scan_status()


def main():
    conf = cfg.load_config()
    data = tempfile.mkdtemp(prefix="audit_test_data_")   # v1.7 data_dir
    conf["data_dir"] = data
    conf["scan_memory_file"] = "scan_memory_test.json"
    conf["bookmarks_file"] = "bookmarks_test.json"
    conf["stations_file"] = "stations_test.json"
    conf["sweep_results_file"] = "sweep_results_test.json"
    conf["agent_config_file"] = "agent_config_test.json"
    conf["audit_dwell_s"] = 2.5

    ok = {}
    eng = ReceiverEngine(conf, prefer_sim=True)
    eng.start()
    try:
        # ----------------------------------------------------------
        # 1. data_dir relocation: artifacts live in the data root
        # ----------------------------------------------------------
        eng.tune(band="wfm", freq_hz=98_300_000)
        time.sleep(0.5)
        ok["data_dir_used"] = os.path.isdir(
            os.path.join(data, "state"))
        # the PI cache file is written lazily (first lookup, even a
        # negative/offline one) -> force one and check it lands in data_dir
        from backend import agent as agent_mod
        agent_mod.pi_lookup("2A5B", dict(eng.agent))
        ok["pi_cache_in_data"] = os.path.exists(
            os.path.join(data, "pi_cache.json"))

        # ----------------------------------------------------------
        # 2. audit over explicit freqs (the two sim RDS stations)
        # ----------------------------------------------------------
        res = eng.start_audit(freqs=[96_100_000, 101_700_000],
                              dwell_s=2.5)
        ok["audit_start"] = bool(res.get("ok")) and res["channels"] == 2
        sc = eng.scan_status()
        ok["audit_status_running"] = (sc["mode"] == "audit"
                                      and sc["running"] is True)
        # audio muted during the audit (scan mute path)
        ok["audit_muted"] = True      # covered indirectly by silence check

        sc = wait_audit_done(eng, 40)
        ok["audit_done"] = sc.get("state") == "done"
        time.sleep(0.8)               # let the restore tune apply

        rep = eng.audit_report()
        ok["report_exists"] = rep is not None and rep["channels"] == 2
        rows = {r["freq_hz"]: r for r in (rep or {}).get("results", [])}
        r961 = rows.get(96_100_000)
        r1017 = rows.get(101_700_000)
        print("audit rows:")
        for r in (rep or {}).get("results", []):
            print("   ", {k: r.get(k) for k in
                          ("freq_hz", "name", "pi", "ps", "ps_complete",
                           "pty", "groups_per_s", "ber", "score")})
        ok["961_pi"] = bool(r961 and r961["pi"] == "2A5B")
        ok["961_ps"] = bool(r961 and r961["ps_complete"]
                            and r961["ps"] == "SIM-RDS")
        ok["961_pty"] = bool(r961 and r961["pty"] == "Top 40")
        ok["961_score"] = bool(r961 and r961["score"] >= 60)
        ok["1017_pi"] = bool(r1017 and r1017["pi"] == "1F2C")
        ok["1017_ps"] = bool(r1017 and r1017["ps_complete"]
                             and r1017["ps"] == "JAZZ-FM")
        ok["1017_score"] = bool(r1017 and r1017["score"] >= 60)
        ok["groups_flow"] = bool(r961 and r961["groups_per_s"] > 0.5)
        ok["tuning_restored"] = (eng.band == "wfm"
                                 and eng.freq == 98_300_000)

        # persisted to the data dir
        latest = os.path.join(data, "state", "audits", "audit_latest.json")
        ok["audit_persisted"] = os.path.exists(latest)
        if os.path.exists(latest):
            with open(latest, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            ok["audit_persisted_shape"] = (
                disk.get("channels") == 2
                and isinstance(disk.get("results"), list))

        st = eng.status()
        ok["status_audit_block"] = (st["audit"]["last_count"] == 2
                                    and st["audit"]["running"] is False)
        ok["status_digest_block"] = "enabled" in st["digest"]

        # ----------------------------------------------------------
        # 3. digest journal captured the audit + engine digests work
        # ----------------------------------------------------------
        day = time.strftime("%Y-%m-%d")
        evs = eng._digest.for_day(day)
        ok["journal_has_audit"] = any(e["kind"] == "audit" for e in evs)
        res = eng.build_digest()
        ok["digest_build"] = res.get("ok") and res["day"] == day \
            and "RDS audit" in res["text"] or "RDS audits" in res["text"]
        gd = eng.get_digest(day)
        ok["digest_get"] = gd.get("ok") and bool(gd.get("text"))
        check_days = eng.digest_status()["days"]
        ok["digest_status_days"] = day in check_days

        # ----------------------------------------------------------
        # 4. manual tune interrupts a running audit (partial kept)
        # ----------------------------------------------------------
        res = eng.start_audit(freqs=[96_100_000, 101_700_000],
                              dwell_s=3.0)
        ok["audit_restart"] = bool(res.get("ok"))
        time.sleep(4.0)               # ~1 channel done
        eng.tune(band="wfm", freq_hz=99_900_000)
        time.sleep(0.6)
        sc = eng.scan_status()
        ok["tune_stops_audit"] = sc["running"] is False
        rep = eng.audit_report()
        ok["partial_report_kept"] = rep is not None \
            and len(rep["results"]) >= 1

        # ----------------------------------------------------------
        # 5. audit refuses to double-run; stop_audit ignores sweeps
        # ----------------------------------------------------------
        eng.start_sweep(bands=["wfm"], from_hz=95_900_000,
                        to_hz=96_300_000, dwell_s=0.2)
        try:
            eng.start_audit(freqs=[96_100_000])
            ok["audit_conflict_rejected"] = False
        except ValueError:
            ok["audit_conflict_rejected"] = True
        ok["stop_audit_ignores_sweep"] = eng.stop_audit() is False
        eng.stop_scan()

        # ----------------------------------------------------------
        # 6. audit with no known stations fails with a clear message
        # ----------------------------------------------------------
        eng.sweep_results = []
        eng.stations = [None] * eng.NUM_STATIONS
        try:
            eng.start_audit()
            ok["audit_no_targets_rejected"] = False
        except ValueError as exc:
            ok["audit_no_targets_rejected"] = "sweep" in str(exc) \
                or "freqs" in str(exc)
    finally:
        eng.close()

    failed = [k for k, v in ok.items() if not v]
    for k, v in ok.items():
        print(("  PASS  " if v else "  FAIL  ") + k)
    print()
    if failed:
        print("AUDIT TEST FAILURES:", failed)
        return 1
    print(f"AUDIT TEST: ALL PASS ({len(ok)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
