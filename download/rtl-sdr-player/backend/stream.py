"""Receiver engine: owns the device, runs the streaming loop, feeds websockets.

The capture loop runs in a daemon thread. Reconfiguration (tuning, band
change, gain) is done via a thread-safe "pending" request that the loop
applies between capture blocks, bumping a generation counter so websocket
clients know to flush their audio buffers.
"""
import threading
import time
from collections import deque

import numpy as np

from . import config as cfg
from . import device as devmod
from . import dsp


class ReceiverEngine:
    def __init__(self, conf, prefer_sim=False):
        self.conf = conf
        self.prefer_sim = prefer_sim
        self.device = None
        self.info = {}
        self.band = conf["default_band"]
        self.freq = cfg.clamp_to_band(conf["default_freq_hz"], self.band)
        self.gain_db = None  # None -> AGC
        self.gen = 0
        self._pending = None
        self._lock = threading.RLock()
        self.psd = None
        self._stop = threading.Event()
        self._thread = None
        self._demod = None
        self._subs = {}  # sub_id -> deque of PCM chunks (per-client)
        self._sub_seq = 0
        self.started = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self.started:
            return
        self.device, self.info = devmod.open_device(prefer_sim=self.prefer_sim)
        self._apply_device_config()
        self._demod = self._make_demod()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sdr-capture")
        self._thread.start()
        self.started = True

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.device:
            self.device.close()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def tune(self, band=None, freq_hz=None):
        with self._lock:
            band = band or self.band
            if band not in cfg.BANDS:
                raise ValueError(f"unknown band: {band}")
            if freq_hz is None:
                freq_hz = self.freq if band == self.band else \
                    cfg.clamp_to_band(self.conf["default_freq_hz"]
                                      if band == self.conf["default_band"]
                                      else (98.3e6 if band == "wfm" else 990e3),
                                      band)
            freq_hz = cfg.clamp_to_band(int(freq_hz), band)
            self._pending = ("tune", band, freq_hz)
        return {"band": band, "freq_hz": freq_hz}

    def set_gain(self, gain_db):
        with self._lock:
            self._pending = ("gain", gain_db)

    def subscribe(self):
        """Register an audio consumer. Each gets its own bounded queue so
        multiple browser tabs / reconnects never steal from each other."""
        with self._lock:
            self._sub_seq += 1
            sub_id = self._sub_seq
            self._subs[sub_id] = deque(maxlen=12)
        return sub_id

    def unsubscribe(self, sub_id):
        with self._lock:
            self._subs.pop(sub_id, None)

    def drain_sub(self, sub_id):
        with self._lock:
            q = self._subs.get(sub_id)
            if q is None:
                return []
            chunks = list(q)
            q.clear()
            return chunks

    def get_psd(self):
        return self.psd

    def current_gen(self):
        return self.gen

    def status(self):
        from . import tv
        with self._lock:
            s = {
                "app": cfg.APP_NAME,
                "version": cfg.VERSION,
                "mode": self.info.get("mode"),
                "device": self.info.get("device"),
                "backend": self.info.get("backend"),
                "note": self.info.get("note"),
                "error": self.info.get("error"),
                "band": self.band,
                "freq_hz": self.freq,
                "gain_db": self.gain_db,
                "gains": (self.device.get_gains() if self.device else []),
                "bands": cfg.BANDS,
                "target_buffer_ms": int(self.conf["audio_target_buffer_s"] * 1000),
                "started": self.started,
            }
            s["tv"] = tv.status()
            return s

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _make_demod(self):
        if self.band == "am":
            return dsp.AmDemod(self.conf["am_sample_rate"])
        return dsp.WfmDemod(self.conf["fm_sample_rate"],
                            self.conf["lo_offset_hz"],
                            self.conf["de_emphasis_us"])

    def _apply_device_config(self):
        d = self.device
        if self.band == "am":
            fs = self.conf["am_sample_rate"]
            d.set_sample_rate(fs)
            d.set_direct_sampling("q")  # v3 direct sampling for HF/AM
            d.set_center_freq(self.freq)  # station at DC
            self._fs = fs
            self._center = self.freq
        else:
            fs = self.conf["fm_sample_rate"]
            d.set_sample_rate(fs)
            d.set_direct_sampling("disabled")
            d.set_center_freq(self.freq - self.conf["lo_offset_hz"])
            self._fs = fs
            self._center = self.freq - self.conf["lo_offset_hz"]
        d.set_gain(self.gain_db)
        self._block_seconds = dsp.BLOCK / float(fs)

    def _loop(self):
        psd_comp = dsp.PsdComputer()
        block_dur = self._block_seconds
        while not self._stop.is_set():
            t0 = time.monotonic()
            # ---- apply pending reconfiguration ----
            with self._lock:
                pending = self._pending
                self._pending = None
            if pending is not None:
                kind = pending[0]
                try:
                    if kind == "tune":
                        _, band, freq = pending
                        self.band, self.freq = band, freq
                        self._apply_device_config()
                        self._demod = self._make_demod()
                        for q in list(self._subs.values()):
                            q.clear()
                        self.psd = None
                        self.gen += 1
                    elif kind == "gain":
                        self.gain_db = pending[1]
                        self.device.set_gain(self.gain_db)
                except Exception as exc:  # keep the loop alive on errors
                    self.info["error"] = f"reconfig failed: {exc}"
                    time.sleep(0.2)
                    continue

            # ---- capture + process ----
            try:
                raw = self.device.read_iq(dsp.BLOCK)
            except Exception as exc:
                self.info["error"] = f"capture failed: {exc}"
                time.sleep(0.3)
                continue
            if len(raw) < dsp.BLOCK // 2:
                time.sleep(0.02)
                continue
            try:
                pcm = self._demod.process(np.asarray(raw, dtype=np.complex64))
                for q in list(self._subs.values()):
                    q.append(pcm)
                self.psd = psd_comp.process(raw, self._fs, self._center,
                                            self.freq)
            except Exception as exc:
                self.info["error"] = f"dsp error: {exc}"
                time.sleep(0.1)

            # ---- pacing (only the simulator produces faster than real time) ----
            elapsed = time.monotonic() - t0
            if elapsed < block_dur:
                time.sleep(block_dur - elapsed)
