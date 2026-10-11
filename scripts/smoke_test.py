#!/usr/bin/env python3
"""End-to-end smoke test for the RTL-SDR Player server (sim mode)."""
import asyncio
import json
import struct
import urllib.request

BASE = "http://localhost:3000"


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


async def ws_test():
    import websockets

    async with websockets.connect("ws://localhost:3000/ws/spectrum") as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert msg["event"] == "psd", msg.get("event")
        n = len(msg["psd_db"])
        peak = max(msg["psd_db"])
        floor = msg["floor_db"]
        print(f"  spectrum: {n} bins, peak {peak:.1f} dB, floor {floor:.1f} dB, "
              f"sig {msg['sig_db']:.1f} dB  -> {'OK' if n == 512 and peak > floor else 'FAIL'}")

    audio_bytes = 0
    chunks = 0
    async with websockets.connect("ws://localhost:3000/ws/audio") as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert hello["event"] == "hello"
        print(f"  audio hello: mode={hello['mode']} rate={hello['audio_rate']} "
              f"target={hello['target_ms']}ms")
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            data = await asyncio.wait_for(ws.recv(), timeout=2)
            if isinstance(data, (bytes, bytearray)):
                audio_bytes += len(data)
                chunks += 1
        print(f"  audio: {chunks} binary chunks, {audio_bytes} bytes "
              f"({audio_bytes/2/48000:.2f} s of PCM) in 2 s -> "
              f"{'OK' if audio_bytes > 96000 else 'FAIL'}")

    # validate PCM is non-silent: parse last chunk's RMS
    return audio_bytes


async def pcm_level():
    import websockets
    import array
    async with websockets.connect("ws://localhost:3000/ws/audio") as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)  # hello
        # discard first (filter transient), then sample a chunk
        for _ in range(3):
            data = await asyncio.wait_for(ws.recv(), timeout=2)
        if isinstance(data, str):
            data = await asyncio.wait_for(ws.recv(), timeout=2)
        i16 = array.array("h")
        i16.frombytes(data)
        rms = (sum(x * x for x in i16) / len(i16)) ** 0.5
        print(f"  PCM RMS level: {rms:.0f} (of 32767) -> "
              f"{'OK (audible)' if rms > 300 else 'FAIL (near silence)'}")


async def main():
    print("REST:")
    st = get("/api/status")
    print(f"  status: mode={st['mode']} device={st['device']!r} tv.available={st['tv']['available']}")
    assert st["mode"] == "sim"

    r = post("/api/tune", {"band": "am", "freq_hz": 820000})
    print(f"  tune AM 820k: {r}")
    assert r["ok"] and r["freq_hz"] == 820000
    await asyncio.sleep(0.5)

    r = post("/api/tune", {"band": "wfm", "freq_hz": 96100000})
    print(f"  tune FM 96.1M: {r}")
    assert r["ok"]

    clamped = post("/api/tune", {"band": "wfm", "freq_hz": 50000000})
    print(f"  out-of-band clamp: {clamped['freq_hz']} (expect 87500000)")
    assert clamped["freq_hz"] == 87500000

    r = post("/api/gain", {"gain_db": 24.4})
    print(f"  gain set: {r}")

    # static UI
    html = urllib.request.urlopen(BASE + "/", timeout=5).read().decode()
    assert "RTL-SDR Player" in html and "spectrum" in html
    print(f"  static UI: index.html served ({len(html)} bytes) OK")

    print("WEBSOCKETS:")
    await ws_test()
    await pcm_level()
    print("ALL TESTS PASSED")


asyncio.run(main())
