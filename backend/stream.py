"""Receiver engine: owns the device, runs the streaming loop, feeds websockets.

The capture loop runs in a daemon thread. Reconfiguration (tuning, band
change, gain) is done via a thread-safe "pending" request that the loop
applies between capture blocks, bumping a generation counter so websocket
clients know to flush their audio buffers.

v1.1 additions:
  * WX (NOAA weather, narrowband FM) and AIR (US airband, AM) bands
  * channel-power squelch with a soft tail, reported to the UI
  * a scanner that hops a channel list, holds on squelch-opening activity
    and logs what it found
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

        # squelch (channel-power gate)
        self.squelch_db = None          # None -> squelch off
        self.squelch_open = True
        self._sq_last_open = 0.0
        self.chan_db = None             # live channel-power reading

        # scanner state
        self._scan = {
            "running": False, "list": None, "idx": 0, "state": "scan",
            "freq_hz": None, "last_active_hz": None, "log": deque(maxlen=8),
            "hop_t": 0.0, "hop_gen": 0, "closed_t": None,
        }

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
            if self._scan["running"]:
                self._scan["running"] = False  # manual tune wins over scanner
            band = band or self.band
            if band not in cfg.BANDS:
                raise ValueError(f"unknown band: {band}")
            if freq_hz is None:
                freq_hz = cfg.band_default_freq(
                    band, self.conf["default_band"],
                    self.conf["default_freq_hz"])
            freq_hz = cfg.clamp_to_band(int(freq_hz), band)
            self._pending = ("tune", band, freq_hz)
        return {"band": band, "freq_hz": freq_hz}

    def set_gain(self, gain_db):
        with self._lock:
            self._pending = ("gain", gain_db)

    def set_squelch(self, db):
        """db: threshold in dB of channel power, or None to disable."""
        with self._lock:
            self.squelch_db = None if db in (None, "off") else float(db)

    def start_scan(self, list_name, squelch_db=None):
        with self._lock:
            if list_name not in cfg.SCAN_LISTS:
                raise ValueError(f"unknown scan list: {list_name}")
            if squelch_db is not None:
                self.squelch_db = float(squelch_db)
            elif self.squelch_db is None:
                self.squelch_db = float(self.conf["squelch_default_db"])
            entry = cfg.SCAN_LISTS[list_name]
            self._scan.update({
                "running": True, "list": list_name, "idx": 0,
                "state": "scan", "freq_hz": entry["freqs"][0],
                "last_active_hz": None, "closed_t": None,
                "hop_t": time.monotonic(), "hop_gen": self.gen + 1,
            })
            self._pending = ("tune", entry["band"], entry["freqs"][0])
        return {"ok": True, "list": list_name}

    def stop_scan(self):
        with self._lock:
            self._scan["running"] = False
            self._scan["state"] = "idle"

    def scan_status(self):
        with self._lock:
            sc = self._scan
            if not sc["running"] and sc["list"] is None:
                return {"running": False}
            entry = cfg.SCAN_LISTS.get(sc["list"], {})
            return {
                "running": sc["running"],
                "list": sc["list"],
                "label": entry.get("label", sc["list"]),
                "idx": sc["idx"],
                "count": len(entry.get("freqs", [])),
                "state": sc["state"],
                "freq_hz": sc["freq_hz"],
                "last_active_hz": sc["last_active_hz"],
                "log": list(sc["log"]),
            }

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

    def current_rds(self):
        """RDS + pilot snapshot (WFM only), or None on other bands."""
        demod = self._demod
        if (self.band != "wfm" or demod is None
                or not hasattr(demod, "rds")):
            return None
        try:
            snap = demod.rds.snapshot()
            snap["pilot"] = bool(demod.pilot.present)
            snap["pilot_db"] = round(demod.pilot.rel_db, 1)
            snap["ppm"] = (round(demod.pilot.ppm, 2)
                           if demod.pilot.present else None)
            return snap
        except Exception:
            return None

    def demod_mode(self):
        return {"wfm": "wfm", "wx": "nbfm", "air": "am", "am": "am"}.get(
            self.band, self.band)

    def status(self):
        from . import tv
        with self._lock:
            s = {
                "app": cfg.APP_NAME,
                "version": cfg.VERSION,
                "region": self.conf.get("region", "US"),
                "de_emphasis_us": self.conf.get("de_emphasis_us", 75),
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
                "scan_lists": {k: v["label"] for k, v in cfg.SCAN_LISTS.items()},
                "squelch_db": self.squelch_db,
                "squelch_open": self.squelch_open,
                "chan_db": self.chan_db,
                "scan": self.scan_status(),
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
            return dsp.AmDemod(self.conf["am_sample_rate"],
                               lo_offset_hz=0.0, channel_bw_hz=7_500)
        if self.band == "air":
            return dsp.AmDemod(self.conf["fm_sample_rate"],
                               lo_offset_hz=self.conf["lo_offset_hz"],
                               channel_bw_hz=5_000)
        if self.band == "wx":
            return dsp.NbfmDemod(self.conf["fm_sample_rate"],
                                 self.conf["lo_offset_hz"])
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

    def _apply_tune(self, band, freq):
        self.band, self.freq = band, freq
        self._apply_device_config()
        self._demod = self._make_demod()
        self.chan_db = None
        for q in list(self._subs.values()):
            q.clear()
        self.psd = None
        self.gen += 1

    def _hop_scan(self):
        """Advance the scanner to the next channel (lock held by caller)."""
        sc = self._scan
        entry = cfg.SCAN_LISTS[sc["list"]]
        sc["idx"] = (sc["idx"] + 1) % len(entry["freqs"])
        freq = entry["freqs"][sc["idx"]]
        sc["freq_hz"] = freq
        sc["state"] = "scan"
        sc["closed_t"] = None
        sc["hop_t"] = time.monotonic()
        sc["hop_gen"] = self.gen + 1
        self._pending = ("tune", entry["band"], freq)

    def _run_scanner(self, now, sq_open):
        """Scanner state machine; returns True when audio should be muted."""
        sc = self._scan
        if not sc["running"]:
            return False
        entry = cfg.SCAN_LISTS[sc["list"]]
        dwell = float(self.conf["scan_dwell_s"])
        release = float(self.conf["scan_release_s"])
        measured = self.gen >= sc["hop_gen"]

        if sc["state"] == "scan":
            if measured and sq_open:
                sc["state"] = "hold"
                sc["last_active_hz"] = sc["freq_hz"]
                sc["closed_t"] = None
                sc["log"].appendleft({"t": time.time(), "hz": sc["freq_hz"]})
                return False
            if now - sc["hop_t"] >= dwell:
                self._hop_scan()
            return True  # muted while hopping
        # holding on an active channel
        if sq_open:
            sc["closed_t"] = None
            return False
        if sc["closed_t"] is None:
            sc["closed_t"] = now
        elif now - sc["closed_t"] >= release:
            self._hop_scan()
        return True  # squelch closed -> muted

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
                        self._apply_tune(pending[1], pending[2])
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
                self.chan_db = getattr(self._demod, "last_chan_db", None)
                rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)) / 32768.0)
                rms_db = 20.0 * np.log10(rms + 1e-9)

                # ---- squelch (channel power vs threshold, soft tail) ----
                now = time.monotonic()
                if self.squelch_db is not None and self.chan_db is not None:
                    if self.chan_db >= self.squelch_db:
                        self.squelch_open = True
                        self._sq_last_open = now
                    elif now - self._sq_last_open > 0.5:
                        self.squelch_open = False
                    sq_open = self.squelch_open
                else:
                    self.squelch_open = True
                    sq_open = True

                # ---- scanner ----
                muted = self._run_scanner(now, sq_open)
                # squelch gates audio on any band while enabled and closed
                if self.squelch_db is not None and not sq_open:
                    muted = True
                if muted:
                    pcm = np.zeros_like(pcm)

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
