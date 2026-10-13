"""v1.5 agent layer tests: command parser, scheduler, keep-locked fill,
LLM naming against a local mock OpenAI-compatible server."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import agent as ag
from backend import config as cfg
from backend import device as devmod
from backend.stream import ReceiverEngine

PASS, FAIL = 0, 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ----------------------------------------------------------------------
class MockEngine:
    """Just enough receiver for execute_command."""

    def __init__(self):
        self.band = "wfm"
        self.freq = 98_300_000
        self.agent = dict(ag.AGENT_DEFAULTS)
        self.tuned = None
        self.sweeps = []
        self.scans = []
        self.stopped = 0
        self.sweep_meta = {"t": None, "bands": None}
        self._stations = [None] * 20

    def tune(self, band=None, freq_hz=None, mode=None):
        self.tuned = (band, freq_hz)
        self.band = band or self.band
        self.freq = freq_hz or self.freq
        return {"band": self.band, "freq_hz": self.freq}

    def start_sweep(self, bands=None, **kw):
        self.sweeps.append(bands or "ALL")
        return {"channels": 123, "bands": bands or ["wfm", "am", "wx", "air"]}

    def start_scan(self, list_name, **kw):
        self.scans.append(list_name)

    def stop_scan(self):
        self.stopped += 1

    def stations_list(self):
        return list(self._stations)

    def sweep_results_list(self):
        return []

    def status(self):
        return {"band": self.band, "freq_hz": self.freq,
                "demod_mode": "wfm", "mode": "sim",
                "stations": list(self._stations)}

    def current_rds(self):
        return None

    def set_agent_settings(self, patch):
        v = float(patch.get("sweep_interval_min") or 0)
        if v and v < 5:
            raise ValueError(
                "re-sweep interval must be >= 5 minutes (or 0 = off)")
        self.agent.update(patch)
        return {}

    def enrich_station_names(self):
        return {"ok": False, "error": "no LLM in mock"}


def test_parser():
    print("== command parser ==")
    eng = MockEngine()
    acfg = dict(ag.AGENT_DEFAULTS)

    r = ag.execute_command(eng, "help", acfg)
    check("help", r["ok"] and r["action"] == "help")

    r = ag.execute_command(eng, "tune to 101.7", acfg)
    check("tune fm", r["ok"] and eng.tuned == ("wfm", 101_700_000),
          str(eng.tuned))

    r = ag.execute_command(eng, "am 1010", acfg)
    check("tune am khz", eng.tuned == ("am", 1_010_000), str(eng.tuned))

    r = ag.execute_command(eng, "go to 121.5", acfg)
    check("tune air", eng.tuned == ("air", 121_500_000), str(eng.tuned))

    r = ag.execute_command(eng, "162.550", acfg)
    check("tune wx", eng.tuned == ("wx", 162_550_000), str(eng.tuned))

    r = ag.execute_command(eng, "scan airband", acfg)
    check("scan airband", eng.scans and eng.scans[-1] == "air_band")

    r = ag.execute_command(eng, "scan noaa", acfg)
    check("scan noaa", eng.scans[-1] == "noaa")

    r = ag.execute_command(eng, "sweep all", acfg)
    check("sweep all", eng.sweeps and eng.sweeps[-1] == "ALL"
          and "123" in r["reply"], r["reply"][:50])

    r = ag.execute_command(eng, "sweep fm wx", acfg)
    check("sweep two bands", eng.sweeps[-1] == ["wfm", "wx"],
          str(eng.sweeps[-1]))

    r = ag.execute_command(eng, "stop", acfg)
    check("stop", eng.stopped == 1)

    r = ag.execute_command(eng, "next station", acfg)
    check("next with empty presets", "No presets" in r["reply"], r["reply"])

    r = ag.execute_command(eng, "sweep every 30 minutes", acfg)
    check("schedule 30", eng.agent.get("sweep_interval_min") == 30.0
          and "30" in r["reply"], r["reply"][:60])

    r = ag.execute_command(eng, "sweeps off", acfg)
    check("schedule off", eng.agent.get("sweep_interval_min") == 0.0)

    r = ag.execute_command(eng, "every 3 minutes", acfg)
    check("interval <5 rejected as reply",
          r["ok"] is False and "5 minutes" in r["reply"], r["reply"][:60])

    r = ag.execute_command(eng, "report", acfg)
    check("report", r["ok"] and "DIAL REPORT" in r["reply"])

    r = ag.execute_command(eng, "what am i listening to", acfg)
    check("status q", r["ok"] and "162.550 MHz" in r["reply"], r["reply"][:60])

    r = ag.execute_command(eng, "name the stations", acfg)
    check("enrich without llm", r["ok"] and "LLM" in r["reply"],
          r["reply"][:60])

    r = ag.execute_command(eng, "frobnicate the widget", acfg)
    check("unknown -> help", r["ok"] is False and "didn't catch" in r["reply"])

    # station cycling
    eng._stations[2] = {"slot": 3, "name": "THREE", "band": "wfm",
                        "freq_hz": 101_700_000, "mode": None}
    r = ag.execute_command(eng, "next", acfg)
    check("next cycles to first preset",
          eng.freq == 101_700_000 and "THREE" in r["reply"], r["reply"])


# ----------------------------------------------------------------------
def make_engine(tmp):
    conf = cfg.load_config()
    eng = ReceiverEngine(conf, prefer_sim=True)
    # redirect every persisted file into the test dir
    for attr, name in (("_st_path", "stations.json"),
                       ("_sweep_path", "sweep_results.json"),
                       ("_agent_path", "agent_config.json"),
                       ("_mem_path", "scan_memory.json"),
                       ("_bmk_path", "bookmarks.json")):
        setattr(eng, attr, os.path.join(tmp, name))
    eng.stations = [None] * ReceiverEngine.NUM_STATIONS
    eng.sweep_results = []
    return eng


def test_fill_keep_locked():
    print("== fill from sweep keeps locked slots ==")
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(tmp)
        eng.sweep_results = [
            {"band": "wfm", "freq_hz": 96_100_000, "db": -8.0,
             "name": "SIM-RDS", "mode": None},
            {"band": "wfm", "freq_hz": 101_700_000, "db": -10.0,
             "name": "JAZZ-FM", "mode": None},
            {"band": "wx", "freq_hz": 162_550_000, "db": -18.0,
             "name": "NOAA WX1", "mode": None},
            {"band": "am", "freq_hz": 820_000, "db": -22.0,
             "name": "AM 820", "mode": None},
        ]
        # user hand-named slot 1 (locked) pointing at JAZZ-FM's channel
        eng.stations[0] = {"slot": 1, "name": "MY JAZZ", "band": "wfm",
                           "freq_hz": 101_700_000, "mode": None,
                           "locked": True}
        placed = eng.fill_stations_from_sweep()
        check("locked slot kept", eng.stations[0]["name"] == "MY JAZZ"
              and eng.stations[0]["locked"] is True)
        names = [(s["name"] if s else None) for s in eng.stations[:5]]
        check("channel not double-booked",
              not any(s and s["freq_hz"] == 101_700_000 for s in eng.stations[1:]),
              str(names))
        check("other slots filled strongest first",
              names[1] == "SIM-RDS" and names[2] == "NOAA WX1"
              and names[3] == "AM 820", str(names))
        # reload from disk keeps the data
        eng2 = make_engine(tmp)
        eng2.stations = eng2._load_stations()
        check("stations persisted",
              eng2.stations[0]["name"] == "MY JAZZ")


def test_scheduler():
    print("== scheduled re-sweep ==")
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(tmp)

        # disabled: tick is a no-op
        eng.agent["sweep_interval_min"] = 0
        eng._scheduler_tick(time.time())
        check("off -> no sweep", not eng._scan["running"])

        # enabled + due + no listeners -> sweep starts
        eng.agent["sweep_interval_min"] = 30
        eng._sched["next_t"] = time.time() - 1
        eng._scheduler_tick(time.time())
        check("due -> sweep started", eng._scan["running"]
              and eng._sched["sweeping"] is True
              and eng._scan["mode"] == "sweep",
              eng._sched["last_note"])

        # while the sweep runs, ticks do nothing
        eng._scheduler_tick(time.time())
        check("sweep in flight not restarted", eng._scan["running"])

        # finish the sweep -> post actions run on the next tick
        eng.stop_scan()
        eng.sweep_results = [{"band": "wfm", "freq_hz": 96_100_000,
                              "db": -8.0, "name": "SIM-RDS", "mode": None}]
        eng._scheduler_tick(time.time())
        check("post-sweep auto-fill",
              eng.stations[0] is not None
              and eng.stations[0]["name"] == "SIM-RDS"
              and not eng._sched["sweeping"],
              eng._sched["last_note"])
        check("next run rescheduled",
              eng._sched["next_t"] is not None
              and eng._sched["next_t"] > time.time() + 25 * 60)

        # listener connected -> deferred
        eng.agent["sweep_interval_min"] = 30
        eng._sched["next_t"] = time.time() - 1
        sub = eng.subscribe()
        eng._scheduler_tick(time.time())
        check("deferred while listening",
              not eng._scan["running"]
              and "deferred" in eng._sched["last_note"],
              eng._sched["last_note"])
        eng.unsubscribe(sub)

        # allow_listeners overrides
        eng.agent["sweep_allow_listeners"] = True
        eng._sched["next_t"] = time.time() - 1
        eng._scheduler_tick(time.time())
        check("allow_listeners sweeps anyway", eng._scan["running"])
        eng.stop_scan()

        # settings validation + persistence
        st = eng.set_agent_settings({"sweep_interval_min": 15,
                                     "sweep_auto_fill": False})
        check("settings applied", st["schedule"]["interval_min"] == 15
              and st["schedule"]["auto_fill"] is False)
        eng3 = make_engine(tmp)
        check("agent config persisted",
              eng3._load_agent_config()["sweep_interval_min"] == 15)
        try:
            eng.set_agent_settings({"sweep_interval_min": 2})
            check("interval 2 rejected", False)
        except ValueError:
            check("interval 2 rejected", True)


# ----------------------------------------------------------------------
class _MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        names = {1: "WQXR", 2: "THE NEWS", 3: "NOAA WX1"}
        content = json.dumps({"stations": [
            {"slot": s, "name": n, "genre": "Test"} for s, n in names.items()
        ]})
        # wrapped in prose + fences to exercise the JSON extractor
        payload = {"choices": [{"message": {
            "content": "Here you go:\n```json\n" + content + "\n```"}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_enrich_mock_llm():
    print("== LLM naming vs mock OpenAI-compatible server ==")
    srv = HTTPServer(("127.0.0.1", 0), _MockLLM)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(tmp)
        eng.stations[0] = {"slot": 1, "name": "FM 96.1", "band": "wfm",
                           "freq_hz": 96_100_000, "locked": False}
        eng.stations[1] = {"slot": 2, "name": "AM 1010", "band": "am",
                           "freq_hz": 1_010_000, "locked": False}
        eng.stations[2] = {"slot": 3, "name": "NOAA WX1", "band": "wx",
                           "freq_hz": 162_550_000, "locked": False}
        acfg = dict(ag.AGENT_DEFAULTS)
        acfg.update({
            "llm_enabled": True,
            "llm_base_url": f"http://127.0.0.1:{srv.server_port}/v1",
            "llm_api_key": "test-key",
            "llm_model": "mock-1",
        })
        res = eng.enrich_station_names(acfg)
        check("enrich ok", res.get("ok") is True and res.get("updated") == 3,
              str(res))
        check("ai_name applied",
              eng.stations[0]["ai_name"] == "WQXR"
              and eng.stations[2]["ai_name"] == "NOAA WX1")
        check("plain name preserved", eng.stations[0]["name"] == "FM 96.1")
        check("genre applied", eng.stations[0].get("genre") == "Test")
        # direct agent function + callsign hints
        cands = ag.pi_callsign_candidates("0x5965")
        check("pi callsign candidate", cands == ["KBUR"], str(cands))
        check("ambiguous pi -> no guess",
              ag.pi_callsign_candidates(30000) == [])
        check("non-us pi -> no guess", ag.pi_callsign_candidates(0x2A5B) == [])
    srv.shutdown()


def main():
    test_parser()
    test_fill_keep_locked()
    test_scheduler()
    test_enrich_mock_llm()
    print()
    if FAIL:
        print(f"AGENT TESTS: {FAIL} FAILURES ({PASS} passed)")
        sys.exit(1)
    print(f"AGENT TESTS: ALL PASS ({PASS} checks)")


if __name__ == "__main__":
    main()
