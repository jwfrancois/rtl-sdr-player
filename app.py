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


@app.post("/api/tune")
async def api_tune(body: dict):
    try:
        applied = engine.tune(band=body.get("band"), freq_hz=body.get("freq_hz"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if "gain_db" in body and body["gain_db"] is not None:
        engine.set_gain(float(body["gain_db"]))
    return {"ok": True, **applied, "gen": engine.current_gen()}


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


@app.post("/api/scan")
async def api_scan(body: dict):
    action = (body.get("action") or "start").lower()
    if action == "stop":
        engine.stop_scan()
        return {"ok": True, "scan": engine.scan_status()}
    list_name = body.get("list") or "noaa"
    try:
        engine.start_scan(list_name, squelch_db=body.get("squelch_db"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "scan": engine.scan_status()}


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
    sub_id = engine.subscribe()
    await ws.send_json({
        "event": "hello", "mode": engine.info.get("mode"),
        "band": engine.band, "freq_hz": engine.freq,
        "target_ms": int(engine.conf["audio_target_buffer_s"] * 1000),
        "audio_rate": 48000,
    })
    try:
        while True:
            gen = engine.current_gen()
            chunks = engine.drain_sub(sub_id)
            if gen != last_gen:
                await ws.send_json({"event": "reset",
                                    "target_ms": int(engine.conf["audio_target_buffer_s"] * 1000)})
                last_gen = gen
            for chunk in chunks:
                await ws.send_bytes(chunk.tobytes())
            await asyncio.sleep(0.01)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.unsubscribe(sub_id)


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
                    "rds": engine.current_rds(),
                    "scan": engine.scan_status(),
                    "squelch_db": engine.squelch_db,
                    "squelch_open": engine.squelch_open,
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
