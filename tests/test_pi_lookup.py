"""v1.6 PI-code online callsign lookup tests: normalization, cache
round-trips, HTML/JSON provider parsing, offline degradation, and the
"lookup pi" agent command. All network access is mocked."""
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import agent as ag
from backend import pi_db

PASS, FAIL = 0, 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


FMSCAN_HTML = """<html><head><title>FMScan - PI F201</title></head><body>
<table><tr><th>Frequency</th><th>Station</th><th>Location</th></tr>
<tr><td>89.10 MHz</td><td><a href="/f/q.html">BBC Radio 1</a></td>
<td>London</td></tr></table></body></html>"""

CUSTOM_JSON = '{"results": [{"callsign": "KQED-FM", "city": "San Francisco"}]}'


class MockEngine:
    """Just enough receiver for execute_command."""

    def __init__(self):
        self.band = "wfm"
        self.freq = 98_300_000
        self.agent = dict(ag.AGENT_DEFAULTS)
        self.tuned = None

    def tune(self, band=None, freq_hz=None, mode=None):
        self.tuned = (band, freq_hz)
        self.band = band or self.band
        self.freq = freq_hz or self.freq
        return {"band": self.band, "freq_hz": self.freq}

    def start_sweep(self, bands=None, **kw):
        return {"channels": 1, "bands": bands or ["wfm"]}

    def start_scan(self, list_name, **kw):
        pass

    def stop_scan(self):
        pass

    def stations_list(self):
        return [None] * 20

    def sweep_results_list(self):
        return []

    def status(self):
        return {"band": self.band, "freq_hz": self.freq,
                "demod_mode": "wfm", "mode": "sim",
                "stations": [None] * 20}

    def current_rds(self):
        return None

    def set_agent_settings(self, patch):
        self.agent.update(patch)
        return {}

    def enrich_station_names(self):
        return {"ok": False, "error": "no LLM in mock"}


def test_norm_pi():
    print("== normalization ==")
    check("lowercase hex", pi_db.norm_pi("2a5b") == "2A5B")
    check("0x prefix", pi_db.norm_pi("0x2A5B") == "2A5B")
    check("short code rejected", pi_db.norm_pi("2A5") is None)
    check("non-hex rejected", pi_db.norm_pi("ZZZZ") is None)
    check("None rejected", pi_db.norm_pi(None) is None)


def test_cache_roundtrip():
    print("== cache ==")
    d = tempfile.mkdtemp()
    cache = pi_db.PiCache(os.path.join(d, "pi_cache_test.json"))
    cache.put("2A5B", "SIM-RDS", "test")
    cache.put("0BAD", None, "miss")
    # fresh instance reads the same file (persistence)
    cache2 = pi_db.PiCache(os.path.join(d, "pi_cache_test.json"))
    hit = cache2.get("2A5B")
    check("positive survives reload", hit and hit["name"] == "SIM-RDS"
          and hit["source"] == "test", str(hit))
    neg = cache2.get("0BAD")
    check("negative survives reload", neg is not None
          and not neg.get("name"), str(neg))
    check("unknown pi misses", cache2.get("DEAD") is None)


def test_providers():
    print("== providers (mocked HTTP) ==")
    d = tempfile.mkdtemp()
    cache = pi_db.PiCache(os.path.join(d, "pi_cache_test.json"))
    acfg = dict(ag.AGENT_DEFAULTS)

    orig_fetch = pi_db._fetch
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "fmscan" in url:
            return FMSCAN_HTML
        raise urllib.error.URLError("blocked")

    pi_db._fetch = fake_fetch
    try:
        res = pi_db.lookup_pi("F201", acfg, cache=cache)
        check("fmscan HTML parsed", res["ok"] and
              res["name"] == "BBC Radio 1" and res["source"] == "fmscan",
              str(res))
        check("title/th noise skipped",
              "FMScan" not in (res["name"] or "")
              and "Frequency" not in (res["name"] or ""))
        # second lookup served from cache (no new HTTP call)
        n = len(calls)
        res2 = pi_db.lookup_pi("F201", acfg, cache=cache)
        check("positive cached", res2["ok"] and res2["cached"]
              and len(calls) == n, str(res2))

        # custom JSON endpoint wins when configured
        acfg2 = dict(acfg, pi_lookup_url="https://db.example/api/{pi}.json")
        calls.clear()
        def fake_fetch2(url):
            calls.append(url)
            if "db.example" in url:
                return CUSTOM_JSON
            raise urllib.error.URLError("blocked")
        pi_db._fetch = fake_fetch2
        res3 = pi_db.lookup_pi("3E20", acfg2, cache=cache)
        check("custom JSON endpoint", res3["ok"]
              and res3["name"] == "KQED-FM" and res3["source"] == "custom",
              str(res3))
        check("custom tried first", calls
              and "db.example" in calls[0], str(calls[:1]))

        # total failure -> graceful miss + negative cache
        calls.clear()
        def fake_fail(url):
            calls.append(url)
            raise urllib.error.URLError("no network")
        pi_db._fetch = fake_fail
        res4 = pi_db.lookup_pi("BEEF", acfg, cache=cache)
        check("offline miss is data, not exception",
              res4["ok"] is False and res4["name"] is None
              and res4["error"], str(res4))
        n = len(calls)
        res5 = pi_db.lookup_pi("BEEF", acfg, cache=cache)
        check("negative cached (no retry storm)",
              res5["ok"] is False and "cached" in (res5["error"] or "")
              and len(calls) == n, f"new calls={len(calls) - n}")

        # disabled in settings -> cache-only
        res6 = pi_db.lookup_pi("1234", dict(acfg, pi_lookup_enabled=False),
                               cache=cache)
        check("disabled answers clearly", res6["ok"] is False
              and "disabled" in (res6["error"] or ""), str(res6))

        # bad pi never raises
        res7 = pi_db.lookup_pi("nothex", acfg, cache=cache)
        check("bad pi rejected", res7["ok"] is False
              and res7["pi"] is None, str(res7))
    finally:
        pi_db._fetch = orig_fetch


def test_agent_command():
    print("== agent lookup command ==")
    eng = MockEngine()
    acfg = dict(ag.AGENT_DEFAULTS)
    orig = ag.pi_lookup
    ag.pi_lookup = lambda pi, cfg, force=False: {
        "ok": True, "pi": pi, "name": "BBC Radio 1", "source": "fmscan",
        "cached": False, "error": None}
    try:
        r = ag.execute_command(eng, "lookup pi F201", acfg)
        check("lookup pi <code>", r["ok"] and r["action"] == "lookup"
              and "BBC Radio 1" in r["reply"], r.get("reply", ""))
        r = ag.execute_command(eng, "who is on pi F201", acfg)
        check("phrased lookup", r["ok"] and "BBC Radio 1" in r["reply"],
              r.get("reply", ""))
        ag.pi_lookup = lambda pi, cfg, force=False: {
            "ok": False, "pi": pi, "name": None, "source": None,
            "cached": True, "error": "no match (cached)"}
        r = ag.execute_command(eng, "pi DEAD", acfg)
        check("miss reply stays friendly", r["ok"] and "no match" in r["reply"],
              r.get("reply", ""))
        # am tune must not be swallowed by the PI pattern
        ag.pi_lookup = orig
        r = ag.execute_command(eng, "am 1010", acfg)
        check("am 1010 still tunes", r["ok"] and r["action"] == "tune"
              and eng.tuned == ("am", 1_010_000), str(eng.tuned))
        r = ag.execute_command(eng, "tune to 101.7", acfg)
        check("fm tune unaffected", r["ok"] and r["action"] == "tune"
              and eng.tuned == ("wfm", 101_700_000), str(eng.tuned))
    finally:
        ag.pi_lookup = orig


if __name__ == "__main__":
    test_norm_pi()
    test_cache_roundtrip()
    test_providers()
    test_agent_command()
    print()
    if FAIL:
        print(f"PI LOOKUP TESTS: {FAIL} FAILED")
        sys.exit(1)
    print(f"PI LOOKUP TESTS: ALL PASS ({PASS})")
