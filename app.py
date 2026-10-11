#!/usr/bin/env python3
"""RTL-SDR Player — local web receiver for the RTL2832U dongle.

Run:  python app.py            (opens http://localhost:8515 in your browser)
      python app.py --sim      (demo mode without a dongle)
"""
import argparse
import asyncio
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import config, stream, tv

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

ARGS = None          # filled in main()
engine: stream.ReceiverEngine = None

app = FastAPI(title=config.APP_NAME, version=config.VERSION)


@app.on_event("startup")
async def _startup():
    global engine
    conf = config.load_config()
    engine = stream.ReceiverEngine(conf, prefer_sim=bool(ARGS and ARGS.sim))
    engine.start()


@app.on_event("shutdown")
async def _shutdown():
    if engine:
        engine.close()


# ----------------------------------------------------------------------
# REST API
# ----------------------------------------------------------------------
@app.get("/api/status")
async def api_status():
    return engine.status()


# ----------------------------------------------------------------------
# Device mode (hardware dongle <-> simulation)
# ----------------------------------------------------------------------
@app.post("/api/device/mode")
async def api_device_mode(body: dict):
    """Hot-swap the receiver between the real dongle and the simulator.

    The new engine is built and started first; only if it really came up in
    the requested mode does it replace the live one (the old engine keeps
    running otherwise, so a failed switch never takes the app down).
    """
    global engine
    want = (body.get("mode") or "").lower()
    if want not in ("hardware", "sim"):
        return JSONResponse(
            {"ok": False, "error": "mode must be 'hardware' or 'sim'"},
            status_code=400)
    cur = engine
    cur_hw = cur.info.get("mode") == "hardware"
    if cur_hw == (want == "hardware"):
        return {"ok": True, "mode": cur.info.get("mode"),
                "device": cur.info.get("device"),
                "note": "already in that mode"}

    conf = config.load_config()
    new = stream.ReceiverEngine(conf, prefer_sim=(want == "sim"))
    new.start()
    got_hw = new.info.get("mode") == "hardware"
    if got_hw != (want == "hardware"):
        # e.g. asked for hardware but the dongle/DLLs are not there
        err = new.info.get("error") or "device not available"
        new.close()
        return JSONResponse({"ok": False, "error": err}, status_code=409)

    engine = new          # atomic-ish swap: new handlers see the new engine
    cur.close()
    return {"ok": True, **{k: new.info.get(k) for k in
                           ("mode", "device", "backend", "note")}}


@app.get("/api/device/dlls")
async def api_device_dlls():
    """Windows DLL diagnostics: what was found where, with the exact error."""
    from backend.device import windows_dll_report
    return windows_dll_report()


@app.post("/api/tune")
async def api_tune(body: dict):
    try:
        applied = engine.tune(band=body.get("band"),
                              freq_hz=body.get("freq_hz"),
                              mode=body.get("mode"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if "gain_db" in body and body["gain_db"] is not None:
        engine.set_gain(float(body["gain_db"]))
    return {"ok": True, **applied, "gen": engine.current_gen()}


@app.post("/api/bfo")
async def api_bfo(body: dict):
    """SSB clarifier: fine tuning offset in Hz (+/-3000)."""
    try:
        hz = engine.set_bfo(float(body.get("hz", 0)))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "invalid bfo"},
                            status_code=400)
    return {"ok": True, "bfo_hz": hz}


@app.post("/api/gain")
async def api_gain(body: dict):
    gain = body.get("gain_db", None)
    engine.set_gain(None if gain in (None, "auto") else float(gain))
    return {"ok": True, "gain_db": gain}


@app.post("/api/squelch")
async def api_squelch(body: dict):
    db = body.get("db", None)
    engine.set_squelch(None if db in (None, "off") else float(db))
    return {"ok": True, "squelch_db": engine.squelch_db}


@app.post("/api/ctcss")
async def api_ctcss(body: dict):
    """CTCSS tone squelch for NBFM (Hz; 0 = off / carrier squelch only)."""
    try:
        hz = engine.set_ctcss(body.get("hz", 0))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "ctcss_hz": hz}


@app.get("/api/bookmarks")
async def api_bookmarks():
    return {"bookmarks": engine.bookmarks_list()}


@app.post("/api/bookmarks")
async def api_bookmarks_add(body: dict):
    """Add a bookmark; omitting band/freq/mode bookmarks the current state."""
    try:
        bm = engine.add_bookmark(body.get("name"), band=body.get("band"),
                                 freq_hz=body.get("freq_hz"),
                                 mode=body.get("mode"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "bookmark": bm, "bookmarks": engine.bookmarks_list()}


@app.post("/api/bookmarks/delete")
async def api_bookmarks_delete(body: dict):
    ok = engine.delete_bookmark(body.get("id"))
    return {"ok": ok, "bookmarks": engine.bookmarks_list()}


@app.post("/api/scan")
async def api_scan(body: dict):
    action = (body.get("action") or "start").lower()
    if action == "stop":
        engine.stop_scan()
        return {"ok": True, "scan": engine.scan_status()}
    if action == "clear_memory":
        engine.clear_memory()
        return {"ok": True, "memory": engine.memory_list()}
    list_name = body.get("list") or "noaa"
    try:
        engine.start_scan(list_name, squelch_db=body.get("squelch_db"),
                          order=body.get("order"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "scan": engine.scan_status()}


@app.get("/api/scan/memory")
async def api_scan_memory():
    return {"memory": engine.memory_list(24)}


# ----------------------------------------------------------------------
# v1.4: all-band measurement sweep + 20 named station presets
# ----------------------------------------------------------------------
@app.post("/api/sweep")
async def api_sweep(body: dict):
    """Sweep every channel of the selected bands, measuring power and
    (on FM) decoding RDS station names. body: {bands: [...],
    threshold_db?, dwell_s?, from_hz?, to_hz?}."""
    try:
        res = engine.start_sweep(
            bands=body.get("bands"),
            threshold_db=(float(body["threshold_db"])
                          if body.get("threshold_db") is not None else None),
            dwell_s=(float(body["dwell_s"])
                     if body.get("dwell_s") is not None else None),
            from_hz=body.get("from_hz"), to_hz=body.get("to_hz"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, **res, "scan": engine.scan_status()}


@app.post("/api/sweep/stop")
async def api_sweep_stop():
    engine.stop_scan()
    return {"ok": True, "scan": engine.scan_status()}


@app.get("/api/sweep/results")
async def api_sweep_results():
    sc = engine.scan_status()
    return {"running": bool(sc.get("running")),
            "mode": sc.get("mode"), "state": sc.get("state"),
            "idx": sc.get("idx", 0), "count": sc.get("count", 0),
            "results": engine.sweep_results_list()}


@app.get("/api/stations")
async def api_stations():
    return {"stations": engine.stations_list()}


@app.post("/api/stations")
async def api_stations_save(body: dict):
    """Save/rename a station slot: {slot, name?, band?, freq_hz?, mode?}.
    Omitting band/freq saves the current tuning (or renames the slot when
    only a name is given)."""
    try:
        st = engine.set_station(body.get("slot"), name=body.get("name"),
                                band=body.get("band"),
                                freq_hz=body.get("freq_hz"),
                                mode=body.get("mode"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "station": st, "stations": engine.stations_list()}


@app.post("/api/stations/delete")
async def api_stations_delete(body: dict):
    try:
        ok = engine.clear_station(body.get("slot"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": ok, "stations": engine.stations_list()}


@app.post("/api/stations/clear")
async def api_stations_clear():
    return {"ok": True, **engine.clear_stations()}


@app.post("/api/stations/fill")
async def api_stations_fill(body: dict):
    """Fill the 20 slots from the last sweep (strongest first,
    adjacent-channel shoulders merged)."""
    try:
        picks = engine.fill_stations_from_sweep(count=body.get("count"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "filled": len(picks),
            "stations": engine.stations_list()}


@app.get("/api/tv/status")
async def api_tv_status():
    return {"status": tv.status(), "channels": tv.list_channels(),
            "scan": tv.scan_message()}


@app.post("/api/tv/scan")
async def api_tv_scan():
    tv.scan_channels()
    return {"ok": True, "scan": tv.scan_message()}


@app.post("/api/tv/watch")
async def api_tv_watch(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "channel name required"},
                            status_code=400)
    try:
        tv.DVBS.start(name)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, "stream": "/api/tv/stream"}


@app.get("/api/tv/stream")
async def api_tv_stream():
    return StreamingResponse(iter(tv.DVBS.read, b""),
                             media_type="video/mp2t")


# ----------------------------------------------------------------------
# WebSockets
# ----------------------------------------------------------------------
@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    last_gen = -1
    eng = engine                     # re-bound when the engine is hot-swapped
    sub_id = eng.subscribe()
    await ws.send_json({
        "event": "hello", "mode": eng.info.get("mode"),
        "band": eng.band, "freq_hz": eng.freq,
        "target_ms": int(eng.conf["audio_target_buffer_s"] * 1000),
        "audio_rate": 48000,
    })
    try:
        while True:
            if engine is not eng:    # device mode switched: re-subscribe
                eng.unsubscribe(sub_id)
                eng = engine
                sub_id = eng.subscribe()
                last_gen = -1
            gen = eng.current_gen()
            chunks = eng.drain_sub(sub_id)
            if gen != last_gen:
                await ws.send_json({"event": "reset",
                                    "target_ms": int(eng.conf["audio_target_buffer_s"] * 1000)})
                last_gen = gen
            for chunk in chunks:
                await ws.send_bytes(chunk.tobytes())
            await asyncio.sleep(0.01)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        eng.unsubscribe(sub_id)


@app.websocket("/ws/spectrum")
async def ws_spectrum(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            psd = engine.get_psd()
            if psd:
                await ws.send_json({
                    "event": "psd",
                    "gen": engine.current_gen(),
                    "band": engine.band,
                    "freq_hz": engine.freq,
                    "demod_mode": engine.demod_mode(),
                    "bfo_hz": engine.bfo_hz,
                    "rds": engine.current_rds(),
                    "scan": engine.scan_status(),
                    "squelch_db": engine.squelch_db,
                    "squelch_open": engine.squelch_open,
                    "ctcss_hz": engine.ctcss_hz,
                    "ctcss_dev_hz": engine._ctcss_dev_hz(),
                    "ctcss_open": engine._ctcss_open(),
                    "chan_db": engine.chan_db,
                    **psd})
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        pass


# Static UI (registered last so API routes take precedence)
app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")


# ----------------------------------------------------------------------
def main():
    global ARGS
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--port", type=int, default=None,
                        help="override the port from config.json")
    parser.add_argument("--sim", action="store_true",
                        help="force simulation mode (no dongle needed)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not auto-open the web browser")
    parser.add_argument("--share", action="store_true",
                        help="listen on all interfaces (accessible from LAN)")
    ARGS = parser.parse_args()

    conf = config.load_config()
    port = ARGS.port or int(conf["port"])
    url = f"http://localhost:{port}"

    print()
    print("  " + "=" * 58)
    print(f"   {config.APP_NAME} v{config.VERSION}")
    print(f"   Open {url} in your browser once the server starts.")
    print("   Press Ctrl+C to quit.")
    print("  " + "=" * 58)
    print()

    if not ARGS.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="0.0.0.0" if ARGS.share else "127.0.0.1",
                port=port, log_level="warning")


if __name__ == "__main__":
    main()
