"""Full smoke test for RTL-SDR Player v1.1 (server + REST + websockets).

Boots the app in sim mode on a test port and verifies:
  1. status: v1.1, US region, new bands + scan lists
  2. RDS over the spectrum websocket (PI/PS/PTY/AF/RT) on the sim station
  3. audio websocket still streams PCM (RMS audible)
  4. squelch REST: opens on WX channel, closes on empty one
  5. scanner REST: NOAA list holds on an active channel and logs it
  6. airband tune + scanner on key channels
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config as cfg  # noqa: E402

BASE = "http://127.0.0.1:8531"
WS = "ws://127.0.0.1:8531"


def rest(path, body=None):
    if body is None:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return json.loads(r.read())
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


async def collect_spectrum(seconds, out):
    async with websockets.connect(WS + "/ws/spectrum") as ws:
        t0 = time.time()
        while time.time() - t0 < seconds:
            out.append(json.loads(await ws.recv()))


async def collect_audio(seconds, out):
    async with websockets.connect(WS + "/ws/audio") as ws:
        await ws.recv()  # hello
        t0 = time.time()
        while time.time() - t0 < seconds:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            if isinstance(msg, bytes):
                out.append(msg)


def rms_of(chunks):
    import numpy as np
    if not chunks:
        return -99.0
    buf = b"".join(chunks)
    i16 = np.frombuffer(buf, dtype=np.int16).astype(np.float64)
    r = float(np.sqrt(np.mean(i16 ** 2))) / 32768.0
    return 20.0 * np.log10(r + 1e-9)


async def main():
    fails = []

    def check(name, ok, extra=""):
        print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + extra if extra else ""))
        if not ok:
            fails.append(name)

    # ---- 1. status ----
    st = rest("/api/status")
    st_bands_wx_presets = st["bands"]["wx"]["presets"]
    check("status version", st["version"] == cfg.VERSION, st["version"])
    check("region US", st.get("region") == "US")
    check("de-emphasis 75 us", st.get("de_emphasis_us") == 75)
    check("bands wx+air present", "wx" in st["bands"] and "air" in st["bands"])
    check("scan lists",
          set(st["scan_lists"]) == {"noaa", "air_key", "air_band",
                                     "fm_band", "am_band"})
    check("wx presets", len(st["bands"]["wx"]["presets"]) == 7)
    check("air modes am/usb/lsb",
          st["bands"]["air"]["modes"] == ["am", "usb", "lsb"])
    rest("/api/scan", {"action": "clear_memory"})  # isolate from prior runs

    # ---- 2. RDS on 96.1 ----
    rest("/api/tune", {"band": "wfm", "freq_hz": 96_100_000})
    frames = []
    await collect_spectrum(11, frames)
    rds = [f.get("rds") for f in frames if f.get("rds")]
    last = rds[-1] if rds else {}
    print("   rds:", {k: last.get(k) for k in
                     ("pi", "ps", "pty", "af_mhz", "groups", "pilot")})
    check("rds pilot detected", bool(last.get("pilot")))
    check("rds pi 2A5B", last.get("pi") == "2A5B", str(last.get("pi")))
    check("rds ps SIM-RDS", last.get("ps") == "SIM-RDS", str(last.get("ps")))
    check("rds pty Top 40", last.get("pty") == "Top 40", str(last.get("pty")))
    check("rds af decoded", 96.2 in (last.get("af_mhz") or []),
          str(last.get("af_mhz")))
    check("rds radiotext", bool(last.get("radiotext")),
          str(last.get("radiotext"))[:40])

    # ---- 3. audio stream ----
    chunks = []
    await collect_audio(2.0, chunks)
    rms = rms_of(chunks)
    check("audio rms audible", rms > -40, f"{rms:.1f} dBFS, {len(chunks)} chunks")

    # ---- 3b. auto-squelch on comm bands (before any manual squelch call) ----
    expected_auto = rest("/api/status")["squelch_default_db"]
    rest("/api/tune", {"band": "wx", "freq_hz": 162_550_000})
    await asyncio.sleep(1.0)
    st = rest("/api/status")
    check("auto squelch enabled on WX", st["squelch_db"] == expected_auto,
          f"{st['squelch_db']} vs default {expected_auto}")
    check("auto squelch open on signal", st["squelch_open"] is True,
          f"chan {st['chan_db']}")
    rest("/api/tune", {"band": "wfm", "freq_hz": 99_900_000})
    await asyncio.sleep(0.8)
    st = rest("/api/status")
    check("squelch auto-off back on FM", st["squelch_db"] is None,
          str(st["squelch_db"]))

    # ---- 4. squelch ----
    rest("/api/tune", {"band": "wx", "freq_hz": 162_550_000})
    rest("/api/squelch", {"db": -20})
    await asyncio.sleep(1.5)
    st = rest("/api/status")
    check("squelch open on WX channel", st["squelch_open"] is True,
          f"chan {st['chan_db']}")
    rest("/api/tune", {"band": "wx", "freq_hz": 162_475_000})
    await asyncio.sleep(1.5)
    st = rest("/api/status")
    check("squelch closed on empty channel", st["squelch_open"] is False,
          f"chan {st['chan_db']}")
    # muted audio is silence
    chunks = []
    await collect_audio(1.0, chunks)
    rms = rms_of(chunks)
    check("muted audio is silence", rms < -55, f"{rms:.1f} dBFS")

    # ---- 5. scanner NOAA ----
    rest("/api/tune", {"band": "wx", "freq_hz": 162_500_000})
    rest("/api/squelch", {"db": -20})
    rest("/api/scan", {"action": "start", "list": "noaa", "squelch_db": -20})
    found = None
    t0 = time.time()
    while time.time() - t0 < 30:
        sc = rest("/api/status")["scan"]
        if sc.get("last_active_hz"):
            found = sc
            break
        await asyncio.sleep(0.5)
    check("scanner found NOAA activity", bool(found),
          str({k: found.get(k) for k in ("state", "freq_hz", "last_active_hz")}
              if found else ""))
    if found:
        check("scanner holds on WX channel",
              found["last_active_hz"] in (162_400_000, 162_550_000),
              str(found["last_active_hz"]))
        check("scanner log has timestamped hit", bool(found["log"]))
    rest("/api/scan", {"action": "stop"})
    sc = rest("/api/status")["scan"]
    check("scanner stops", sc["running"] is False)

    # manual tune must stop the scanner
    rest("/api/scan", {"action": "start", "list": "noaa"})
    rest("/api/tune", {"band": "wfm", "freq_hz": 99_900_000})
    sc = rest("/api/status")["scan"]
    check("manual tune stops scanner", sc["running"] is False)

    # ---- 6. airband ----
    rest("/api/squelch", {"db": None})
    rest("/api/tune", {"band": "air", "freq_hz": 121_500_000})
    await asyncio.sleep(1.2)
    st = rest("/api/status")
    check("airband 121.5 opens squelch", st["chan_db"] > -20,
          f"chan {st['chan_db']}")
    chunks = []
    await collect_audio(1.5, chunks)
    rms = rms_of(chunks)
    check("airband audio audible", rms > -40, f"{rms:.1f} dBFS")

    rest("/api/scan", {"action": "start", "list": "air_key", "squelch_db": -20})
    found = None
    t0 = time.time()
    while time.time() - t0 < 25:
        sc = rest("/api/status")["scan"]
        if sc.get("last_active_hz"):
            found = sc
            break
        await asyncio.sleep(0.4)
    rest("/api/scan", {"action": "stop"})
    check("airband scanner finds activity",
          bool(found) and found["last_active_hz"] in
          (121_500_000, 122_800_000, 123_100_000),
          str(found and found["last_active_hz"]))

    # ---- 7. scan memory + smart order ----
    mem = rest("/api/scan/memory")["memory"]
    check("scan memory has entries", len(mem) >= 1,
          str([(e["hz"], e["hits"]) for e in mem][:4]))
    check("memory sorted by hits",
          all(mem[i]["hits"] >= mem[i + 1]["hits"]
              for i in range(len(mem) - 1)),
          str([(e["hz"], e["hits"]) for e in mem][:4]))
    # smart order ranks only the scan list's channels: expected top NOAA channel
    wx_hz = [p["hz"] for p in st_bands_wx_presets]
    hits_for = {h: next((e["hits"] for e in mem if e["hz"] == h), 0)
                for h in wx_hz}
    last_for = {h: next((e["last"] for e in mem if e["hz"] == h), 0.0)
                for h in wx_hz}
    expected_top = max(wx_hz, key=lambda h: (hits_for[h], last_for[h]))
    rest("/api/scan", {"action": "start", "list": "noaa", "order": "smart"})
    await asyncio.sleep(0.5)
    sc = rest("/api/status")["scan"]
    check("smart order accepted", sc.get("order") == "smart", str(sc.get("order")))
    check("smart scan starts on most-active NOAA channel",
          sc["freq_hz"] == expected_top,
          f"{sc['freq_hz']} vs expected {expected_top}")
    rest("/api/scan", {"action": "stop"})
    rest("/api/scan", {"action": "clear_memory"})
    mem = rest("/api/scan/memory")["memory"]
    check("memory clears", len(mem) == 0, str(mem))

    # ---- 7b. CTCSS tone squelch (NBFM): 162.550 carries 141.3 in the sim ----
    rest("/api/tune", {"band": "wx", "freq_hz": 162_550_000})
    rest("/api/squelch", {"db": -20})
    await asyncio.sleep(1.0)
    r = rest("/api/ctcss", {"hz": 123.0})
    check("ctcss tone accepted", r.get("ok") is True
          and r.get("ctcss_hz") == 123.0, str(r))
    await asyncio.sleep(1.5)
    chunks = []
    await collect_audio(1.5, chunks)
    rms_wrong_tone = rms_of(chunks)
    check("ctcss wrong tone mutes audio", rms_wrong_tone < -55,
          f"{rms_wrong_tone:.1f} dBFS")
    st = rest("/api/status")
    check("ctcss gate reported closed", st["ctcss_open"] is False
          and st["ctcss_hz"] == 123.0, f"dev={st['ctcss_dev_hz']}")
    rest("/api/ctcss", {"hz": 141.3})
    await asyncio.sleep(1.8)
    chunks = []
    await collect_audio(1.5, chunks)
    rms_right_tone = rms_of(chunks)
    check("ctcss correct tone passes audio",
          rms_right_tone > rms_wrong_tone + 15,
          f"{rms_right_tone:.1f} vs {rms_wrong_tone:.1f} dBFS")
    import urllib.error
    try:
        rest("/api/ctcss", {"hz": 123.4})
        check("ctcss invalid tone rejected", False)
    except urllib.error.HTTPError as exc:
        check("ctcss invalid tone rejected", exc.code == 400,
              f"HTTP {exc.code}")
    rest("/api/ctcss", {"hz": 0})

    # ---- 7c. SDR#-style bookmarks ----
    n0 = len(rest("/api/bookmarks")["bookmarks"])
    r = rest("/api/bookmarks", {"name": "WX1 sim", "band": "wx",
                                "freq_hz": 162_550_000, "mode": "nbfm"})
    check("bookmark add (explicit)", r.get("ok") is True,
          str(r.get("bookmark")))
    rest("/api/tune", {"band": "wfm", "freq_hz": 99_900_000})
    await asyncio.sleep(0.3)
    r2 = rest("/api/bookmarks", {})   # default = current state
    check("bookmark add (default current)",
          r2.get("ok") is True and r2["bookmark"]["band"] == "wfm"
          and r2["bookmark"]["freq_hz"] == 99_900_000,
          str(r2.get("bookmark")))
    bl = rest("/api/bookmarks")["bookmarks"]
    check("bookmark list grows + sorted",
          len(bl) == n0 + 2
          and all(bl[i]["freq_hz"] <= bl[i + 1]["freq_hz"]
                  for i in range(len(bl) - 1)),
          str([(b["band"], b["freq_hz"]) for b in bl]))
    bwx = next(b for b in bl if b["name"] == "WX1 sim")
    rest("/api/tune", {"band": bwx["band"], "freq_hz": bwx["freq_hz"],
                       "mode": bwx["mode"]})
    await asyncio.sleep(0.8)
    st = rest("/api/status")
    check("bookmark jump tunes + restores mode",
          st["freq_hz"] == bwx["freq_hz"] and st["band"] == "wx"
          and st["demod_mode"] == "nbfm",
          f"{st['band']} {st['freq_hz']} {st['demod_mode']}")
    try:
        rest("/api/bookmarks", {"band": "shortwave", "freq_hz": 1_000_000})
        check("bookmark invalid band rejected", False)
    except urllib.error.HTTPError as exc:
        check("bookmark invalid band rejected", exc.code == 400,
              f"HTTP {exc.code}")
    rest("/api/bookmarks/delete", {"id": bwx["id"]})
    b2 = next(b for b in rest("/api/bookmarks")["bookmarks"]
              if b["freq_hz"] == 99_900_000)
    rest("/api/bookmarks/delete", {"id": b2["id"]})
    check("bookmark delete",
          len(rest("/api/bookmarks")["bookmarks"]) == n0)

    # ---- 8. SSB (USB/LSB) on the airband ----
    r = rest("/api/tune", {"band": "air", "freq_hz": 119_200_000,
                           "mode": "usb"})
    await asyncio.sleep(1.2)
    st = rest("/api/status")
    check("ssb mode applied", st["demod_mode"] == "usb", str(st["demod_mode"]))
    chunks = []
    await collect_audio(1.5, chunks)
    rms = rms_of(chunks)
    check("ssb usb audio audible", rms > -40, f"{rms:.1f} dBFS")
    # wrong sideband must stay quiet (sideband selection works)
    rest("/api/tune", {"band": "air", "freq_hz": 119_200_000, "mode": "lsb"})
    await asyncio.sleep(1.2)
    chunks = []
    await collect_audio(1.5, chunks)
    rms_wrong = rms_of(chunks)
    check("wrong sideband rejected", rms_wrong < rms - 15,
          f"lsb {rms_wrong:.1f} vs usb {rms:.1f} dBFS")
    # clarifier endpoint
    rest("/api/bfo", {"hz": 500})
    st = rest("/api/status")
    check("bfo set + reported", st["bfo_hz"] == 500, str(st["bfo_hz"]))
    rest("/api/bfo", {"hz": 0})
    # LSB station
    rest("/api/tune", {"band": "air", "freq_hz": 123_450_000, "mode": "lsb"})
    await asyncio.sleep(1.2)
    chunks = []
    await collect_audio(1.5, chunks)
    rms = rms_of(chunks)
    check("ssb lsb audio audible", rms > -40, f"{rms:.1f} dBFS")
    # mode validation (server answers 400)
    try:
        bad = rest("/api/tune", {"band": "wfm", "freq_hz": 99_900_000,
                                 "mode": "usb"})
        check("invalid mode rejected", bad.get("ok") is False, str(bad))
    except urllib.error.HTTPError as exc:
        check("invalid mode rejected", exc.code == 400, f"HTTP {exc.code}")
    rest("/api/tune", {"band": "wfm", "freq_hz": 99_900_000})

    # ---- 9. device mode API (v1.3.1) ----
    dll = rest("/api/device/dlls")
    check("dll report is a dict", isinstance(dll, dict), type(dll).__name__)
    # already-sim -> asking for sim again succeeds without a swap
    r = rest("/api/device/mode", {"mode": "sim"})
    check("mode sim -> sim is a no-op", r.get("ok") is True, str(r))
    # asking for hardware in a dongle-less environment must fail SAFELY:
    # HTTP 409 + reason, and the app must still be alive in sim afterwards
    try:
        r = rest("/api/device/mode", {"mode": "hardware"})
        sw_ok = r.get("ok") is True and r.get("mode") == "hardware"
        check("hardware switch (dongle present?)", sw_ok, str(r))
        if sw_ok:  # a real dongle answered: put the sim back
            rest("/api/device/mode", {"mode": "sim"})
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        check("hardware switch fails safely (409 + reason)",
              exc.code == 409 and bool(body.get("error")),
              f"HTTP {exc.code}: {body.get('error', '')[:60]}")
    st = rest("/api/status")
    check("app alive in sim after failed switch",
          st.get("mode") == "sim", str(st.get("mode")))
    try:
        rest("/api/device/mode", {"mode": "banana"})
        check("invalid mode rejected", False)
    except urllib.error.HTTPError as exc:
        check("invalid mode rejected", exc.code == 400, f"HTTP {exc.code}")

    # ---- 10. all-band sweep + 20 station presets (v1.4) ----
    r = rest("/api/sweep", {"bands": ["wfm"], "from_hz": 95_900_000,
                            "to_hz": 96_300_000, "dwell_s": 0.2})
    check("segment sweep starts", r.get("ok") is True
          and r.get("channels") == 5, str(r.get("channels")))
    deadline = time.time() + 25
    sr = rest("/api/sweep/results")
    while time.time() < deadline and sr.get("running"):
        await asyncio.sleep(0.4)
        sr = rest("/api/sweep/results")
    results = sr.get("results", [])
    hit = next((x for x in results if x["freq_hz"] == 96_100_000), None)
    check("sweep completes, station found (shoulders merged)",
          not sr.get("running") and len(results) == 1 and hit is not None,
          str([(x["freq_hz"], x["db"]) for x in results]))
    check("sweep auto-names via RDS",
          bool(hit) and hit["name"] == "SIM-RDS",
          str(hit and hit["name"]))
    r = rest("/api/stations/fill", {})
    check("fill presets from sweep", r.get("ok") is True
          and r.get("filled") == 1 and r["stations"][0]["name"] == "SIM-RDS"
          and sum(1 for s in r["stations"] if s) == 1, str(r.get("filled")))
    st = rest("/api/status")
    check("status carries 20 slots", len(st.get("stations", [])) == 20)
    r = rest("/api/stations", {"slot": 3, "band": "wfm",
                               "freq_hz": 96_100_000})
    check("save station (live RDS name)", r.get("ok") is True
          and r["station"]["name"] == "SIM-RDS",
          str(r.get("station", {}).get("name")))
    r = rest("/api/stations", {"slot": 3, "name": "My RDS"})
    check("rename station", r.get("ok") is True
          and r["station"]["name"] == "My RDS")
    r = rest("/api/stations/delete", {"slot": 3})
    check("delete station", r.get("ok") is True
          and r["stations"][2] is None)
    try:
        rest("/api/stations", {"slot": 21})
        check("bad slot rejected", False)
    except urllib.error.HTTPError as exc:
        check("bad slot rejected", exc.code == 400, f"HTTP {exc.code}")
    r = rest("/api/stations/clear", {})
    check("clear stations", r.get("ok") is True)
    try:
        rest("/api/sweep", {"bands": ["banana"]})
        check("unknown sweep band rejected", False)
    except urllib.error.HTTPError as exc:
        check("unknown sweep band rejected", exc.code == 400,
              f"HTTP {exc.code}")
    r = rest("/api/sweep/stop", {})
    check("sweep stop when idle ok", r.get("ok") is True)

    # ---- 11. agent: config, commands, report, schedule (v1.5) ----
    # deterministic start: scheduler off, no LLM (leftovers from earlier
    # manual sessions must not flip these checks)
    rest("/api/agent/config", {"sweep_interval_min": 0,
                               "sweep_auto_fill": True,
                               "sweep_allow_listeners": False,
                               "llm_enabled": False})
    r = rest("/api/agent/config")
    ag0 = r.get("agent") or {}
    check("agent config exposed",
          "schedule" in ag0 and "llm" in ag0
          and ag0["schedule"].get("interval_min") == 0,
          json.dumps(ag0.get("schedule", {}))[:80])
    r = rest("/api/agent/command", {"text": "status"})
    check("agent command status", r.get("ok") is True
          and "presets" in r.get("reply", ""), r.get("reply", "")[:60])
    r = rest("/api/agent/command", {"text": "tune to 101.7"})
    check("agent command tune", r.get("ok") is True
          and r.get("action") == "tune", r.get("reply", "")[:60])
    await asyncio.sleep(0.6)
    st = rest("/api/status")
    check("agent tune applied on engine", st["band"] == "wfm"
          and abs(st["freq_hz"] - 101_700_000) < 500,
          f"{st['band']} {st['freq_hz']}")
    r = rest("/api/agent/command", {"text": "sweep every 15 minutes"})
    check("agent schedule via command",
          r.get("ok") is True and "15" in r.get("reply", ""),
          r.get("reply", "")[:60])
    st = rest("/api/status")
    check("schedule visible in status",
          st["agent"]["schedule"]["interval_min"] == 15
          and st["agent"]["schedule"]["enabled"] is True
          and st["agent"]["schedule"]["next_run_in_s"] is not None)
    r = rest("/api/agent/config", {"sweep_interval_min": 0})
    check("schedule off again", r["agent"]["schedule"]["enabled"] is False)
    r = rest("/api/agent/command", {"text": "report"})
    check("agent report via command", r.get("ok") is True
          and "DIAL REPORT" in r.get("reply", ""), r.get("reply", "")[:60])
    r = json.loads(urllib.request.urlopen(BASE + "/api/agent/report",
                                          timeout=5).read())
    check("report endpoint", r.get("ok") is True
          and bool(r.get("text")) and r.get("ai") is False)
    r = rest("/api/agent/command", {"text": "rescan"})
    check("rescan alias starts sweep", r.get("ok") is True
          and r.get("action") == "sweep", r.get("reply", "")[:60])
    rest("/api/sweep/stop", {})
    r = rest("/api/agent/command", {"text": "help"})
    check("agent help", r.get("ok") is True and "sweep" in r.get("reply", ""))
    r = rest("/api/agent/command", {"text": "gibberish xyzzy"})
    check("unknown command stays friendly", r.get("ok") is False
          and "catch" in r.get("reply", ""), r.get("reply", "")[:50])
    r = rest("/api/agent/enrich", {})
    check("enrich without LLM answers clearly", r.get("ok") is False
          and "LLM" in r.get("error", ""), r.get("error", "")[:60])
    # restore pre-sweep-ish tuning for later/next runs
    rest("/api/tune", {"band": "wfm", "freq_hz": 98_300_000})

    print()
    if fails:
        print("SMOKE TEST FAILURES:", fails)
        return 1
    print("SMOKE TEST: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
