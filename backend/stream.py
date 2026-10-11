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

v1.3 additions:
  * CTCSS tone squelch on the NBFM path (two-stage gate: carrier power
    AND the selected sub-audible tone; the scanner only holds on
    tone-matching channels)
  * SDR#-style bookmarks: named band/freq/mode memories persisted to
    bookmarks.json
"""
import json
import os
import threading
import time
from collections import deque

import numpy as np

from . import config as cfg
from . import device as devmod
from . import dsp

# Demodulation modes available per band (UI mode buttons come from
# cfg.BANDS[band]["modes"]; the SSB modes use the phasing demodulator).
DEFAULT_MODE = {"wfm": "wfm", "wx": "nbfm", "am": "am", "air": "am"}


class ReceiverEngine:
    def __init__(self, conf, prefer_sim=False):
        self.conf = conf
        self.prefer_sim = prefer_sim
        self.device = None
        self.info = {}
        self.band = conf["default_band"]
        self.freq = cfg.clamp_to_band(conf["default_freq_hz"], self.band)
        self.mode = None                # None -> band default
        self.bfo_hz = 0.0               # SSB clarifier offset
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
        self._sq_user = False           # user has taken squelch control
        self.chan_db = None             # live channel-power reading

        # CTCSS tone squelch (NBFM only): 0 disables, else a cfg.CTCSS_TONES
        self.ctcss_hz = 0.0

        # station/scan memory: {"freqs": {hz_str: {hz, hits, last}}}
        self._mem_path = os.path.join(cfg.ROOT,
                                      conf.get("scan_memory_file",
                                               "scan_memory.json"))
        self.memory = self._load_memory()

        # SDR#-style bookmarks: [{id, name, band, freq_hz, mode, added}]
        self._bmk_path = os.path.join(cfg.ROOT,
                                      conf.get("bookmarks_file",
                                               "bookmarks.json"))
        self.bookmarks = self._load_bookmarks()
        self._bmk_seq = max([int(b.get("id", 0)) for b in self.bookmarks],
                            default=0)

        # scanner state
        self._scan = {
            "running": False, "list": None, "idx": 0, "state": "scan",
            "freq_hz": None, "last_active_hz": None, "log": deque(maxlen=8),
            "hop_t": 0.0, "hop_gen": 0, "closed_t": None,
            "freqs": [], "order": "linear",
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
    def tune(self, band=None, freq_hz=None, mode=None):
        with self._lock:
            if self._scan["running"]:
                self._scan["running"] = False  # manual tune wins over scanner
            band = band or self.band
            if band not in cfg.BANDS:
                raise ValueError(f"unknown band: {band}")
            if mode is not None:
                if mode not in cfg.BANDS[band]["modes"]:
                    raise ValueError(
                        f"mode {mode} not available on band {band}")
                self.mode = mode
            elif self.mode not in (None, *cfg.BANDS[band]["modes"]):
                self.mode = None  # previous mode invalid here -> default
            if freq_hz is None:
                freq_hz = cfg.band_default_freq(
                    band, self.conf["default_band"],
                    self.conf["default_freq_hz"])
            freq_hz = cfg.clamp_to_band(int(freq_hz), band)
            # comm bands (NBFM weather / airband AM) default to squelch ON
            # so the channel stays quiet between transmissions
            if band != self.band and not self._sq_user:
                auto = self.conf.get("squelch_auto_bands") or {}
                self.squelch_db = (float(self.conf["squelch_default_db"])
                                   if auto.get(band) else None)
            self._pending = ("tune", band, freq_hz)
        return {"band": band, "freq_hz": freq_hz,
                "mode": self.demod_mode()}

    def set_gain(self, gain_db):
        with self._lock:
            self._pending = ("gain", gain_db)

    def set_squelch(self, db):
        """db: threshold in dB of channel power, or None to disable."""
        with self._lock:
            self.squelch_db = None if db in (None, "off") else float(db)
            self._sq_user = True   # user decision beats band defaults

    def set_ctcss(self, hz):
        """CTCSS tone squelch for NBFM (Hz; 0 = carrier squelch only).

        Selecting a tone implies squelch on: the tone gate sits behind the
        channel-power gate, so it needs a carrier threshold to gate first.
        """
        hz = float(hz or 0.0)
        if hz < 0:
            raise ValueError("ctcss hz must be >= 0")
        if hz and hz not in cfg.CTCSS_TONES:
            raise ValueError(f"unknown CTCSS tone: {hz}")
        with self._lock:
            self.ctcss_hz = hz
            if hz and self.squelch_db is None:
                self.squelch_db = float(self.conf["squelch_default_db"])
                self._sq_user = True
            if self._demod is not None and hasattr(self._demod, "ctcss"):
                self._demod.ctcss.set_tone(hz)
        return hz

    def set_bfo(self, hz):
        """SSB clarifier offset (Hz); applied to the live demodulator."""
        hz = max(-3000.0, min(3000.0, float(hz)))
        with self._lock:
            self.bfo_hz = hz
            self._pending = ("bfo", hz)
        return hz

    def start_scan(self, list_name, squelch_db=None, order=None):
        with self._lock:
            if list_name not in cfg.SCAN_LISTS:
                raise ValueError(f"unknown scan list: {list_name}")
            if squelch_db is not None:
                self.squelch_db = float(squelch_db)
                self._sq_user = True
            elif self.squelch_db is None:
                self.squelch_db = float(self.conf["squelch_default_db"])
                self._sq_user = True
            entry = cfg.SCAN_LISTS[list_name]
            freqs = list(entry["freqs"])
            # smart order: visit the channels with remembered activity first
            order = order if order in ("linear", "smart") else "linear"
            if order == "smart":
                mem = self.memory.get("freqs", {})
                rank = {int(k): (v.get("hits", 0), v.get("last", 0.0))
                        for k, v in mem.items()}
                freqs.sort(key=lambda f: (-rank.get(f, (0, 0.0))[0],
                                          -rank.get(f, (0, 0.0))[1]))
            self._scan.update({
                "running": True, "list": list_name, "idx": 0,
                "state": "scan", "freq_hz": freqs[0],
                "last_active_hz": None, "closed_t": None,
                "hop_t": time.monotonic(), "hop_gen": self.gen + 1,
                "freqs": freqs, "order": order,
            })
            self._pending = ("tune", entry["band"], freqs[0])
        return {"ok": True, "list": list_name, "order": order}

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
                "count": len(sc["freqs"]),
                "state": sc["state"],
                "order": sc["order"],
                "freq_hz": sc["freq_hz"],
                "last_active_hz": sc["last_active_hz"],
                "log": list(sc["log"]),
                "memory": self.memory_list(),
            }

    # ------------------------------------------------------------------
    # scan memory (persisted station activity)
    # ------------------------------------------------------------------
    def _load_memory(self):
        try:
            with open(self._mem_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("freqs"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"freqs": {}}

    def _save_memory(self):
        try:
            tmp = self._mem_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.memory, fh)
            os.replace(tmp, self._mem_path)
        except OSError:
            pass  # memory is a convenience; never break the capture loop

    def _record_hit(self, hz):
        entry = self.memory["freqs"].setdefault(
            str(int(hz)), {"hz": int(hz), "hits": 0, "last": 0.0})
        entry["hits"] += 1
        entry["last"] = round(time.time(), 1)
        self._save_memory()

    def memory_list(self, limit=12):
        """Most-active channels first (hits desc, then most recently heard)."""
        entries = sorted(self.memory.get("freqs", {}).values(),
                         key=lambda e: (-e.get("hits", 0),
                                        -e.get("last", 0.0)))
        return entries[:limit]

    def clear_memory(self):
        with self._lock:
            self.memory = {"freqs": {}}
            self._save_memory()
        return {"ok": True}

    # ------------------------------------------------------------------
    # bookmarks (SDR#-style named memories, persisted)
    # ------------------------------------------------------------------
    def _load_bookmarks(self):
        try:
            with open(self._bmk_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [b for b in data
                        if isinstance(b, dict) and "freq_hz" in b]
        except (OSError, ValueError):
            pass
        return []

    def _save_bookmarks(self):
        try:
            tmp = self._bmk_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.bookmarks, fh, indent=1)
            os.replace(tmp, self._bmk_path)
        except OSError:
            pass  # bookmarks are a convenience; never break the capture loop

    @staticmethod
    def _default_bm_name(band, freq_hz):
        if band == "am":
            return f"{freq_hz / 1e3:.0f} kHz"
        return f"{freq_hz / 1e6:.3f} MHz"

    def add_bookmark(self, name=None, band=None, freq_hz=None, mode=None):
        with self._lock:
            band = band or self.band
            if band not in cfg.BANDS:
                raise ValueError(f"unknown band: {band}")
            freq_hz = cfg.clamp_to_band(
                int(freq_hz if freq_hz is not None else self.freq), band)
            mode = mode or (self.demod_mode() if band == self.band else None)
            if mode not in cfg.BANDS[band]["modes"]:
                mode = None
            # idempotent on (band, freq): refresh name/mode instead of dupes
            for b in self.bookmarks:
                if b["band"] == band and b["freq_hz"] == freq_hz:
                    b["name"] = (name or b["name"]).strip()[:40]
                    b["mode"] = mode or b.get("mode")
                    self._save_bookmarks()
                    return b
            bm = {
                "id": self._bmk_seq + 1,
                "name": (str(name).strip() if name
                         else self._default_bm_name(band, freq_hz))[:40],
                "band": band,
                "freq_hz": freq_hz,
                "mode": mode,
                "added": round(time.time(), 1),
            }
            self._bmk_seq = bm["id"]
            self.bookmarks.append(bm)
            if len(self.bookmarks) > 200:
                self.bookmarks = self.bookmarks[-200:]
            self._save_bookmarks()
            return bm

    def delete_bookmark(self, bm_id):
        with self._lock:
            before = len(self.bookmarks)
            self.bookmarks = [b for b in self.bookmarks
                              if b.get("id") != bm_id]
            if len(self.bookmarks) != before:
                self._save_bookmarks()
                return True
        return False

    def bookmarks_list(self):
        return sorted(self.bookmarks,
                      key=lambda b: (b["band"], b["freq_hz"]))

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
        mode = self.mode or DEFAULT_MODE.get(self.band, self.band)
        return mode

    def _ctcss_state(self):
        """(dev_hz, open) from the live NBFM demodulator, or (None, False)."""
        d = self._demod
        if hasattr(d, "ctcss"):
            return getattr(d.ctcss, "dev_hz", 0.0), bool(d.ctcss.open)
        return None, False

    def _ctcss_dev_hz(self):
        st = self._ctcss_state()
        return round(st[0], 1) if st[0] is not None else None

    def _ctcss_open(self):
        return self._ctcss_state()[1]

    def status(self):
        from . import tv
        with self._lock:
            s = {
                "app": cfg.APP_NAME,
                "version": cfg.VERSION,
                "region": self.conf.get("region", "US"),
                "de_emphasis_us": self.conf.get("de_emphasis_us", 75),
                "mode": self.info.get("mode"),
                "demod_mode": self.demod_mode(),
                "bfo_hz": self.bfo_hz,
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
                "squelch_default_db": self.conf.get("squelch_default_db"),
                "squelch_open": self.squelch_open,
                "ctcss_hz": self.ctcss_hz,
                "ctcss_tones": cfg.CTCSS_TONES,
                "ctcss_dev_hz": self._ctcss_dev_hz(),
                "ctcss_open": self._ctcss_open(),
                "chan_db": self.chan_db,
                "scan": self.scan_status(),
                "bookmarks": self.bookmarks_list(),
                "target_buffer_ms": int(self.conf["audio_target_buffer_s"] * 1000),
                "started": self.started,
            }
            s["tv"] = tv.status()
            return s

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _make_demod(self):
        mode = self.demod_mode()
        if self.band == "am":
            if mode in ("usb", "lsb"):
                return dsp.SsbDemod(self.conf["am_sample_rate"],
                                    lo_offset_hz=0.0, side=mode,
                                    bfo_hz=self.bfo_hz)
            return dsp.AmDemod(self.conf["am_sample_rate"],
                               lo_offset_hz=0.0, channel_bw_hz=7_500)
        if self.band == "air":
            if mode in ("usb", "lsb"):
                return dsp.SsbDemod(self.conf["fm_sample_rate"],
                                    self.conf["lo_offset_hz"], side=mode,
                                    bfo_hz=self.bfo_hz)
            return dsp.AmDemod(self.conf["fm_sample_rate"],
                               lo_offset_hz=self.conf["lo_offset_hz"],
                               channel_bw_hz=5_000)
        if self.band == "wx":
            d = dsp.NbfmDemod(self.conf["fm_sample_rate"],
                              self.conf["lo_offset_hz"])
            d.ctcss.set_tone(self.ctcss_hz)
            return d
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
        sc["idx"] = (sc["idx"] + 1) % len(sc["freqs"])
        freq = sc["freqs"][sc["idx"]]
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
                self._record_hit(sc["freq_hz"])
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
                    elif kind == "bfo":
                        if hasattr(self._demod, "set_bfo"):
                            self._demod.set_bfo(pending[1])
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

                # ---- squelch (channel power vs threshold, hysteresis + tail)
                now = time.monotonic()
                if self.squelch_db is not None and self.chan_db is not None:
                    hyst = float(self.conf.get("squelch_hysteresis_db", 4.0))
                    if self.chan_db >= self.squelch_db:
                        self.squelch_open = True
                        self._sq_last_open = now
                    elif (self.chan_db < self.squelch_db - hyst
                          and now - self._sq_last_open > 0.5):
                        self.squelch_open = False
                    sq_open = self.squelch_open
                else:
                    self.squelch_open = True
                    sq_open = True

                # ---- CTCSS tone gate (NBFM): carrier open AND tone match
                eff_open = sq_open
                if self.ctcss_hz > 0.0 and self.demod_mode() == "nbfm":
                    eff_open = sq_open and self._ctcss_open()

                # ---- scanner (holds must respect the full gate) ----
                muted = self._run_scanner(now, eff_open)
                # squelch gates audio on any band while enabled and closed
                if self.squelch_db is not None and not eff_open:
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
