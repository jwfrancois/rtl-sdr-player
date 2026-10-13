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
import re
import threading
import time
from collections import deque

import numpy as np

from . import agent as agent_mod
from . import config as cfg
from . import device as devmod
from . import digest as digest_mod
from . import dsp
from . import pi_db as pi_db_mod

# Demodulation modes available per band (UI mode buttons come from
# cfg.BANDS[band]["modes"]; the SSB modes use the phasing demodulator).
DEFAULT_MODE = {"wfm": "wfm", "wx": "nbfm", "am": "am", "air": "am"}


class ReceiverEngine:
    def __init__(self, conf, prefer_sim=False):
        self.conf = conf
        self.prefer_sim = prefer_sim
        self.device = None
        self.info = {}
        # v1.7: every runtime artifact resolves here (empty = next to
        # app.py, as before; Docker sets it to the mounted volume)
        self._data_dir = conf.get("data_dir") or cfg.ROOT
        self._state_dir = os.path.join(self._data_dir, "state")
        self.band = conf["default_band"]
        self.freq = cfg.clamp_to_band(conf["default_freq_hz"], self.band)
        self.mode = None                # None -> band default
        self.bfo_hz = 0.0               # SSB clarifier offset
        self.gain_db = None  # None -> AGC
        self.gen = 0
        self._pending = None
        self._lock = threading.RLock()
        self.psd = None
        self.view = None               # prebuilt spectrum-WS snapshot (v1.6)
        self._stop = threading.Event()
        self._thread = None
        self._demod = None
        self._subs = {}  # sub_id -> deque of PCM chunks (per-client)
        self._sub_seq = 0
        self.started = False

        # v1.3.5: USB ingestion runs in its own thread so Python DSP time
        # never eats into the realtime capture budget (the old serial loop
        # consumed block_dur + dsp_time per block -> sustained deficit ->
        # client jitter-buffer underruns -> choppy audio on real hardware).
        self._reader = None
        self._raw_q = deque()      # full IQ blocks waiting for the DSP thread
        self._raw_max = 10         # ~1.1 s of headroom at 2.4 MSPS
        self._pause_req = threading.Event()   # DSP asks the reader to park
        self._parked = threading.Event()      # reader confirms it parked
        self._resume = threading.Event()      # DSP releases the reader
        self._residue = None        # partial (short) read accumulation
        self.stats = {"usb_overflows": 0, "short_reads": 0, "dsp_ms": 0.0}

        # squelch (channel-power gate)
        self.squelch_db = None          # None -> squelch off
        self.squelch_open = True
        self._sq_last_open = 0.0
        self._sq_user = False           # user has taken squelch control
        self._sq_was_muted = False      # last block was gated (edge fades)
        self.chan_db = None             # live channel-power reading

        # CTCSS tone squelch (NBFM only): 0 disables, else a cfg.CTCSS_TONES
        self.ctcss_hz = 0.0

        # station/scan memory: {"freqs": {hz_str: {hz, hits, last}}}
        self._mem_path = self._data_file(
            conf.get("scan_memory_file", "scan_memory.json"))
        self.memory = self._load_memory()

        # SDR#-style bookmarks: [{id, name, band, freq_hz, mode, added}]
        self._bmk_path = self._data_file(
            conf.get("bookmarks_file", "bookmarks.json"))
        self.bookmarks = self._load_bookmarks()
        self._bmk_seq = max([int(b.get("id", 0)) for b in self.bookmarks],
                            default=0)

        # v1.4: the 20 named station presets ("STATIONS" panel). Each slot
        # is {slot, name, band, freq_hz, mode} or None when empty.
        self._st_path = self._data_file(
            conf.get("stations_file", "stations.json"))
        self.stations = self._load_stations()

        # v1.6: change feed for browser notifications. Every mutation of
        # the preset bank bumps _stations_rev and leaves a diff in
        # _stations_change; the UI polls /api/status and turns rev jumps
        # into desktop notifications / toasts.
        self._stations_rev = 0
        self._stations_change = None
        self._pi_thread = None

        # v1.4: last all-band sweep results (persisted, feeds "fill presets")
        self._sweep_path = self._data_file(
            conf.get("sweep_results_file", "sweep_results.json"))
        self.sweep_results = self._load_sweep_results()
        self.sweep_meta = {"t": None, "bands": None}

        # v1.5: agent layer — scheduled background re-sweeps, LLM naming,
        # natural-language commands. Settings persist to agent_config.json.
        self._agent_path = self._data_file(
            conf.get("agent_config_file", "agent_config.json"))
        self.agent = self._load_agent_config()
        self._sched = {"next_t": None, "last_run": None,
                       "last_note": "never ran", "sweeping": False}
        self._sched_stop = threading.Event()
        self._sched_thread = None

        # v1.7: scheduled-sweep change digest (journal -> daily text/email)
        self._digest = digest_mod.DigestJournal(self._state_dir)
        self._digest_store = digest_mod.DigestStore(self._state_dir)
        self._digest_state = {"next_t": None, "last_built": None,
                              "last_note": "never built"}
        # v1.7: spectrum-wide RDS audit (latest report metadata)
        self._audit_meta = {"last_t": None, "count": 0, "note": "never run"}

        # scanner state
        #   mode "live"  - hop/hold on squelch activity (v1.1 behaviour)
        #   mode "sweep" - measure every channel across the selected bands,
        #                  record power + RDS name, no holds, mute audio
        # "chans" is a list of (band, freq_hz) tuples for both modes.
        self._scan = {
            "running": False, "list": None, "mode": "live", "label": None,
            "idx": 0, "state": "idle",
            "freq_hz": None, "last_active_hz": None, "log": deque(maxlen=8),
            "hop_t": 0.0, "hop_gen": 0, "closed_t": None,
            "freqs": [], "chans": [], "order": "linear",
            # sweep-only fields
            "bands": [], "threshold_db": None, "dwell": 0.35,
            "rds_wait": 1.6, "cur_db": None, "rds_name": None,
            "rds_pi": None, "restore": None, "t0": None,
            "trigger": "manual",
            # v1.7 audit-only fields
            "audit_cur": None,
        }

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _data_file(self, name):
        """Runtime artifact path inside the data root (v1.7 data_dir)."""
        return os.path.join(self._data_dir, name)

    def start(self):
        if self.started:
            return
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            os.makedirs(self._state_dir, exist_ok=True)
        except OSError:
            pass
        self.device, self.info = devmod.open_device(prefer_sim=self.prefer_sim)
        pi_db_mod.init_cache(self._data_file("pi_cache.json"))
        self._apply_device_config()
        self._demod = self._make_demod()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sdr-capture")
        self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                        name="sdr-usb")
        self._thread.start()
        self._reader.start()
        # v1.5: background re-sweep scheduler (no-op while disabled)
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._scheduler_loop,
                                              daemon=True,
                                              name="agent-scheduler")
        self._sched_thread.start()
        self.started = True

    def close(self):
        self._stop.set()
        self._sched_stop.set()
        self._pause_req.clear()
        self._resume.set()          # unblock a parked reader
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._reader:
            self._reader.join(timeout=2.0)
        if self._sched_thread:
            self._sched_thread.join(timeout=2.0)
        if self.device:
            self.device.close()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def tune(self, band=None, freq_hz=None, mode=None):
        with self._lock:
            if self._scan["running"]:
                # manual tune wins over scanner/sweep/audit; a measurement
                # keeps the partial results but does not yank the tuner back
                self._scan["running"] = False
                self._scan["state"] = "idle"
                if self._scan["mode"] == "sweep":
                    self._close_sweep(save=True, restore=None)
                elif self._scan["mode"] == "audit":
                    self._close_audit(save=True, restore=None)
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
            self._begin_scan(
                mode="live", chans=[(entry["band"], f) for f in freqs],
                list_name=list_name, label=entry.get("label", list_name),
                order=order)
            self._scan["threshold_db"] = (float(self.squelch_db)
                                          if self.squelch_db is not None
                                          else float(self.conf["squelch_default_db"]))
        return {"ok": True, "list": list_name, "order": order}

    def start_sweep(self, bands=None, threshold_db=None, dwell_s=None,
                    from_hz=None, to_hz=None, am_sensitivity=None,
                    trigger="manual"):
        """All-band measurement sweep (v1.4, AM sensitivity v1.6).

        Tunes across every channel of the selected bands, records the peak
        channel power and (on FM) the RDS station name of each hot channel,
        then leaves a sorted result list behind for the STATIONS panel's
        "fill presets" action. Audio is muted while sweeping; the previous
        tuning is restored at the end (or on stop / manual tune).

        AM handling: a fast probe pass measures the AM noise floor just
        before the AM segment, and the AM threshold becomes floor + margin
        where margin is set by am_sensitivity (high=3 dB, medium=6 dB,
        low=12 dB). A fixed absolute threshold cannot work on AM: the
        noise floor moves with daytime/nighttime propagation and local
        QRM, so a static cut either drowns every weak carrier at night or
        floods the results with receiver noise by day.
        """
        req_bands = list(bands) if bands is not None \
            else list(cfg.SWEEP_BAND_ORDER)
        unknown = [b for b in req_bands if b not in cfg.BANDS]
        if unknown:
            raise ValueError(f"unknown sweep bands: {unknown}")
        # keep the documented order, dropping bands the caller omitted
        ordered = [b for b in cfg.SWEEP_BAND_ORDER if b in req_bands]
        if not ordered:
            raise ValueError("no valid bands to sweep")
        chans = []
        lo = int(from_hz) if from_hz is not None else None
        hi = int(to_hz) if to_hz is not None else None
        for band in ordered:
            for f in cfg._band_channels(band):
                if lo is not None and f < lo:
                    continue
                if hi is not None and f > hi:
                    continue
                chans.append((band, f))
        if not chans:
            raise ValueError("sweep range selects no channels")
        am_sens = str(am_sensitivity or self.agent.get("am_sensitivity")
                      or "medium").lower()
        if am_sens not in ("low", "medium", "high"):
            am_sens = "medium"
        am_chans = [f for b, f in chans if b == "am"]
        with self._lock:
            thr = (float(threshold_db) if threshold_db is not None
                   else float(self.conf["squelch_default_db"]))
            dwell = (float(dwell_s) if dwell_s is not None
                     else float(self.conf["sweep_dwell_s"]))
            dwell = max(0.12, min(2.0, dwell))
            self._begin_scan(
                mode="sweep", chans=chans, list_name=None,
                label="Sweep: " + "+".join(
                    cfg.BANDS[b]["label"] for b in ordered),
                order="linear")
            self._scan.update({
                "bands": ordered, "threshold_db": thr, "dwell": dwell,
                "rds_wait": float(self.conf["sweep_rds_wait_s"]),
                "restore": (self.band, self.freq), "t0": time.time(),
                "results": [], "trigger": trigger,
                # v1.6: adaptive AM sensitivity (noise-floor probe pass)
                "am_sens": am_sens if am_chans else None,
                "am_probe_chans": am_chans,
                "am_thr_db": None, "am_floor_db": None,
                "am_probed": not am_chans, "probe": None,
                "rds_pty": None,
            })
        return {"ok": True, "bands": ordered, "channels": len(chans),
                "threshold_db": thr, "dwell_s": dwell,
                "am_sensitivity": am_sens}

    def start_audit(self, freqs=None, dwell_s=None):
        """Spectrum-wide RDS audit (v1.7).

        Dwells on each known FM carrier and scores its RDS reception:
        PI stability, PS completeness, programme type, RadioText, group
        rate and block-error ratio combine into a 0-100 quality score.
        Default scope: every station of the last sweep plus the FM
        presets; pass freqs for a custom set. Audio is muted while
        auditing; the previous tuning is restored at the end (or on
        stop / manual tune, keeping the partial report).
        """
        with self._lock:
            if self._scan["running"]:
                raise ValueError(
                    "a scan/sweep/audit is already running - stop it first")
            dwell = (float(dwell_s) if dwell_s is not None
                     else float(self.conf["audit_dwell_s"]))
            dwell = max(1.0, min(30.0, dwell))
            if freqs:
                chans = [("wfm", cfg.clamp_to_band(int(f), "wfm"))
                         for f in freqs]
                chans = list(dict.fromkeys(chans))
            else:
                seen = [int(r["freq_hz"]) for r in self._dedupe_results(
                    [r for r in (self.sweep_results or [])
                     if r.get("band") == "wfm" and r.get("db") is not None])]
                for st in self.stations:
                    if (st and st.get("band") == "wfm"
                            and not any(abs(int(st["freq_hz"]) - f) < 500
                                        for f in seen)):
                        seen.append(int(st["freq_hz"]))
                chans = [("wfm", f) for f in sorted(set(seen))]
            chans = chans[:80]   # safety valve: 206 ch x 30 s would run 1.7 h
            if not chans:
                raise ValueError(
                    "no FM stations known - run a sweep first, or pass freqs")
            self._begin_scan(mode="audit", chans=chans, list_name=None,
                             label="RDS audit", order="linear")
            self._scan.update({
                "dwell": dwell, "rds_wait": 0.0,
                "restore": (self.band, self.freq), "t0": time.time(),
                "results": [], "trigger": "manual", "audit_cur": None,
            })
        return {"ok": True, "channels": len(chans), "dwell_s": dwell,
                "seconds_est": round(len(chans) * (dwell + 0.45))}

    def _begin_scan(self, mode, chans, list_name, label, order):
        """Common scan/sweep setup (lock held by caller)."""
        self._scan.update({
            "running": True, "list": list_name, "mode": mode, "label": label,
            "idx": 0, "state": "scan", "freq_hz": chans[0][1],
            "last_active_hz": None, "closed_t": None,
            "hop_t": time.monotonic(), "hop_gen": self.gen + 1,
            "freqs": [f for _, f in chans], "chans": list(chans),
            "order": order, "cur_db": None, "rds_name": None,
            "rds_pi": None, "rds_pty": None,
        })
        band0, freq0 = chans[0]
        self._pending = ("tune", band0, freq0)

    def stop_scan(self):
        with self._lock:
            if not self._scan["running"]:
                self._scan["state"] = "idle"
                return
            mode = self._scan["mode"]
            restore = self._scan.get("restore") \
                if mode in ("sweep", "audit") else None
            self._scan["running"] = False
            self._scan["state"] = "idle"
            if mode == "sweep":
                self._close_sweep(save=True, restore=restore)
            elif mode == "audit":
                self._close_audit(save=True, restore=restore)

    def stop_audit(self):
        """Stop the running audit only (a sweep/scan is left untouched).
        Returns True when an audit was actually stopped."""
        with self._lock:
            if not (self._scan["running"] and self._scan["mode"] == "audit"):
                return False
        self.stop_scan()
        return True

    def scan_status(self):
        with self._lock:
            sc = self._scan
            if not sc["running"] and not sc["chans"]:
                return {"running": False, "mode": sc["mode"],
                        "results": len(sc.get("results", []))
                        if sc["mode"] == "sweep" else 0}
            return {
                "running": sc["running"],
                "list": sc["list"],
                "mode": sc["mode"],
                "label": sc.get("label") or sc["list"] or "scan",
                "idx": sc["idx"],
                "count": len(sc["chans"]),
                "state": sc["state"],
                "order": sc["order"],
                "freq_hz": sc["freq_hz"],
                "last_active_hz": sc["last_active_hz"],
                "log": list(sc["log"]),
                "memory": self.memory_list(),
                "results": len(sc.get("results", [])),
                "bands": sc.get("bands") or [],
                "trigger": sc.get("trigger"),
                "dwell_s": (round(float(sc.get("dwell") or 0), 1)
                            if sc["mode"] in ("sweep", "audit") else None),
                "am_sens": sc.get("am_sens"),
                "am_floor_db": sc.get("am_floor_db"),
                "am_thr_db": sc.get("am_thr_db"),
                "probing": sc.get("probe") is not None,
                "probe_i": (sc["probe"]["i"] if sc.get("probe") else 0),
                "probe_count": (len(sc["probe"]["chans"])
                                if sc.get("probe") else 0),
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

    # ------------------------------------------------------------------
    # station presets: 20 named slots ("STATIONS" panel, v1.4)
    # ------------------------------------------------------------------
    NUM_STATIONS = 20

    def _load_stations(self):
        try:
            with open(self._st_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and len(data) == self.NUM_STATIONS:
                return data
        except (OSError, ValueError):
            pass
        return [None] * self.NUM_STATIONS

    def _save_stations(self):
        try:
            tmp = self._st_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.stations, fh, indent=1)
            os.replace(tmp, self._st_path)
        except OSError:
            pass  # presets are a convenience; never break the capture loop

    def stations_list(self):
        return list(self.stations)

    def set_station(self, slot, name=None, band=None, freq_hz=None,
                    mode=None):
        """Save a station into slot 1..20. Omitting band/freq saves the
        current tuning (naming it from live RDS when available); passing
        only a name renames what is already in the slot."""
        if not 1 <= int(slot) <= self.NUM_STATIONS:
            raise ValueError(f"slot must be 1..{self.NUM_STATIONS}")
        slot = int(slot)
        with self._lock:
            cur = self.stations[slot - 1]
            rename_only = bool(name) and band is None and freq_hz is None
            if band is None and freq_hz is None and name:
                # rename (keep the station already in the slot)
                if cur is None:
                    raise ValueError("empty slot: nothing to rename")
                band, freq_hz = cur["band"], cur["freq_hz"]
                mode = mode or cur.get("mode")
            else:
                band = band or self.band
                if band not in cfg.BANDS:
                    raise ValueError(f"unknown band: {band}")
                freq_hz = cfg.clamp_to_band(
                    int(freq_hz if freq_hz is not None else self.freq), band)
                mode = mode or (self.demod_mode() if band == self.band
                                else None)
                if mode not in cfg.BANDS[band]["modes"]:
                    mode = None
            if not name:
                # 1) live RDS identity - only valid for the channel that is
                #    actually tuned right now
                if band == self.band == "wfm" and freq_hz == self.freq:
                    name = self._live_station_name()
                # 2) a sweep that already identified this channel keeps
                pi = None
                if not name:
                    for r in self.sweep_results or []:
                        if (r.get("band") == band
                                and r.get("freq_hz") == freq_hz):
                            if r.get("name"):
                                name = r["name"]
                            pi = r.get("pi")
                            break
                name = name or self._default_station_name(band, freq_hz)
            else:
                pi = None
            st = {"slot": slot, "name": str(name).strip()[:32] or "?",
                  "band": band, "freq_hz": freq_hz, "mode": mode,
                  "added": round(time.time(), 1),
                  # user-typed name -> auto-fill must keep this slot
                  "locked": bool(name),
                  "pi": pi}
            old = self.stations
            self.stations[slot - 1] = st
            self._save_stations()
            self._commit_stations(old, "rename" if rename_only else "save")
            return st

    def clear_station(self, slot):
        if not 1 <= int(slot) <= self.NUM_STATIONS:
            raise ValueError(f"slot must be 1..{self.NUM_STATIONS}")
        with self._lock:
            gone = self.stations[int(slot) - 1]
            old = self.stations
            self.stations[int(slot) - 1] = None
            self._save_stations()
            if gone is not None:
                self._commit_stations(old, "delete")
        return gone is not None

    def clear_stations(self):
        with self._lock:
            old = self.stations
            self.stations = [None] * self.NUM_STATIONS
            self._save_stations()
            self._commit_stations(old, "clear")
        return {"ok": True}

    def _commit_stations(self, old, source):
        """Record a preset-bank change for the UI notification feed (lock
        held by caller). old is the previous stations list."""
        def brief(st):
            return {"slot": st["slot"], "name": st.get("name"),
                    "band": st.get("band"), "freq_hz": st.get("freq_hz")}

        def key(st):
            return (st["band"], int(st["freq_hz"])) if st else None

        oldmap = {key(s): s for s in (old or []) if s}
        newmap = {key(s): s for s in (self.stations or []) if s}
        added = [brief(s) for k, s in newmap.items() if k not in oldmap]
        removed = [brief(s) for k, s in oldmap.items() if k not in newmap]
        renamed = []
        for k, s in newmap.items():
            o = oldmap.get(k)
            if o is None:
                continue
            if (o.get("name") != s.get("name")
                    or o.get("ai_name") != s.get("ai_name")):
                renamed.append({"slot": s["slot"], "from": o.get("name"),
                                "to": s.get("ai_name") or s.get("name")})
        self._stations_rev += 1
        self._stations_change = {
            "t": round(time.time(), 1), "source": source,
            "added": added, "removed": removed, "renamed": renamed,
        }
        try:
            self._digest.record("stations", source=source, added=added,
                                removed=removed, renamed=renamed)
        except Exception:
            pass                  # the digest journal never breaks presets

    def fill_stations_from_sweep(self, count=None, keep_locked=True,
                                 source="fill"):
        """Populate the 20 slots from the last sweep results.

        Strongest stations first, one per channel (adjacent-channel
        shoulders are merged so one broadcast does not eat two slots).
        With keep_locked=True, slots the user named by hand ("locked")
        survive: they keep their name, and if their channel is still in
        the sweep results that pick is consumed so the channel is not
        double-booked into another slot.
        """
        results = [r for r in (self.sweep_results or [])
                   if r.get("db") is not None]
        if not results:
            raise ValueError("no sweep results yet — run a sweep first")
        count = int(count or self.NUM_STATIONS)
        count = max(1, min(self.NUM_STATIONS, count))
        picks = self._dedupe_results(results)
        with self._lock:
            stations = [None] * self.NUM_STATIONS
            used = set()
            if keep_locked:
                for i, cur in enumerate(self.stations):
                    if not (cur and cur.get("locked")):
                        continue
                    stations[i] = dict(cur)
                    match = next(
                        (j for j, r in enumerate(picks)
                         if j not in used and r.get("band") == cur["band"]
                         and abs(r["freq_hz"] - cur["freq_hz"]) < 500), None)
                    if match is not None:
                        used.add(match)
            placed = []
            free_slots = iter(i for i in range(self.NUM_STATIONS)
                              if stations[i] is None)
            for j, r in enumerate(picks):
                if j in used or len(placed) >= count:
                    continue
                i = next(free_slots, None)
                if i is None:
                    break
                band = r["band"]
                stations[i] = {
                    "slot": i + 1,
                    "name": (r.get("name") or
                             self._default_station_name(band,
                                                        r["freq_hz"]))[:32],
                    "band": band,
                    "freq_hz": int(r["freq_hz"]),
                    "mode": r.get("mode"),
                    "added": round(time.time(), 1),
                    "locked": False,
                    "pi": r.get("pi"),
                }
                placed.append(r)
            old = self.stations
            self.stations = stations
            self._save_stations()
            self._commit_stations(old, source)
        return placed

    @staticmethod
    def _dedupe_results(results):
        """Sort by power desc, then drop same-band neighbours within ~2
        channel steps (a strong FM carrier also lights its neighbours)."""
        step = {"wfm": 200_000, "am": 10_000, "wx": 25_000, "air": 25_000}
        ordered = sorted(results, key=lambda r: -float(r["db"]))
        picked = []
        for r in ordered:
            near = next((p for p in picked
                         if p["band"] == r["band"]
                         and abs(p["freq_hz"] - r["freq_hz"])
                         < 1.9 * step.get(r["band"], 25_000)), None)
            if near is None:
                picked.append(r)
        return picked

    def sweep_results_list(self):
        """Last sweep's results, strongest first, adjacent-channel
        shoulders merged (one broadcast -> one row)."""
        raw = [r for r in (self.sweep_results or [])
               if r.get("db") is not None]
        return self._dedupe_results(raw)

    def _load_sweep_results(self):
        try:
            with open(self._sweep_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("results"),
                                                     list):
                self.sweep_meta = {"t": data.get("t"),
                                   "bands": data.get("bands")}
                return data["results"]
        except (OSError, ValueError):
            pass
        return []

    def _save_sweep_results(self, meta):
        try:
            tmp = self._sweep_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"t": meta.get("t"), "bands": meta.get("bands"),
                           "results": self.sweep_results}, fh, indent=1)
            os.replace(tmp, self._sweep_path)
        except OSError:
            pass  # sweep results are a convenience; never break the loop

    def _default_station_name(self, band, freq_hz):
        """Sensible name when nothing better (RDS) is available."""
        if band == "wfm":
            return f"FM {freq_hz / 1e6:.1f}"
        if band == "am":
            return f"AM {freq_hz / 1e3:.0f}"
        if band == "wx":
            return "NOAA " + cfg.WX_NAMES.get(freq_hz,
                                              f"{freq_hz / 1e6:.3f}")
        if band == "air":
            known = {p["hz"]: p["label"].split(" ", 1)[-1]
                     for p in cfg.BANDS["air"]["presets"]}
            return "AIR " + known.get(freq_hz, f"{freq_hz / 1e6:.3f}")
        return f"{freq_hz / 1e6:.3f}"

    def _live_station_name(self):
        """The RDS PS of whatever is tuned right now (WFM only)."""
        snap = self.current_rds()
        if snap and snap.get("ps"):
            return str(snap["ps"]).strip()[:32]
        return None

    # ------------------------------------------------------------------
    # v1.5 agent layer: scheduled re-sweeps, LLM naming, settings
    # ------------------------------------------------------------------
    def _load_agent_config(self):
        merged = dict(agent_mod.AGENT_DEFAULTS)
        try:
            with open(self._agent_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k in merged:
                    if k in data:
                        merged[k] = data[k]
        except (OSError, ValueError):
            pass
        return merged

    def _save_agent_config(self):
        try:
            tmp = self._agent_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.agent, fh, indent=1)
            os.replace(tmp, self._agent_path)
        except OSError:
            pass  # agent settings are a convenience; never break the loop

    def agent_status(self):
        with self._lock:
            a = dict(self.agent)
            sched = dict(self._sched)
        interval = float(a.get("sweep_interval_min") or 0)
        now = time.time()
        key = str(a.get("llm_api_key") or "")
        return {
            "schedule": {
                "enabled": interval > 0,
                "interval_min": interval,
                "next_run_in_s": (max(0, round(sched["next_t"] - now))
                                  if interval > 0 and sched.get("next_t")
                                  else None),
                "last_run": sched.get("last_run"),
                "last_note": sched.get("last_note"),
                "sweeping": bool(sched.get("sweeping")),
                "auto_fill": bool(a.get("sweep_auto_fill")),
                "allow_listeners": bool(a.get("sweep_allow_listeners")),
                "bands": a.get("sweep_bands"),
            },
            "llm": {
                "enabled": bool(a.get("llm_enabled")),
                "configured": bool(a.get("llm_base_url") and key),
                "model": a.get("llm_model") or "",
                "base_url": a.get("llm_base_url") or "",
                "key_hint": ("..." + key[-4:]) if key else "",
            },
            # v1.6
            "am_sensitivity": a.get("am_sensitivity") or "medium",
            "pi_lookup_enabled": bool(a.get("pi_lookup_enabled", True)),
            "pi_lookup_url": a.get("pi_lookup_url") or "",
            # v1.7
            "audit_dwell_s": float(a.get("audit_dwell_s") or 5.0),
            "digest": {k: v for k, v in self.digest_status().items()
                       if k != "days"},
        }

    def set_agent_settings(self, patch):
        """Validate + persist an agent-settings patch; returns new status."""
        a = dict(self.agent)
        if "sweep_interval_min" in patch:
            v = float(patch["sweep_interval_min"] or 0)
            if v and v < 5:
                raise ValueError(
                    "re-sweep interval must be >= 5 minutes (or 0 = off)")
            a["sweep_interval_min"] = v
        if "sweep_bands" in patch:
            b = patch["sweep_bands"]
            if b is not None:
                unknown = [x for x in b if x not in cfg.BANDS]
                if unknown:
                    raise ValueError(f"unknown sweep bands: {unknown}")
                b = list(b)
            a["sweep_bands"] = b
        for k in ("sweep_auto_fill", "sweep_allow_listeners",
                  "auto_enrich", "llm_enabled"):
            if k in patch:
                a[k] = bool(patch[k])
        for k in ("llm_base_url", "llm_model"):
            if k in patch:
                a[k] = str(patch[k] or "").strip()
        if patch.get("llm_api_key") is not None:
            a["llm_api_key"] = str(patch["llm_api_key"]).strip()
        # v1.6: AM sweep sensitivity + PI online lookup
        if "am_sensitivity" in patch:
            v = str(patch["am_sensitivity"] or "medium").lower()
            if v not in ("low", "medium", "high"):
                raise ValueError("am_sensitivity must be low/medium/high")
            a["am_sensitivity"] = v
        if "pi_lookup_enabled" in patch:
            a["pi_lookup_enabled"] = bool(patch["pi_lookup_enabled"])
        if "pi_lookup_url" in patch:
            a["pi_lookup_url"] = str(patch["pi_lookup_url"] or "").strip()
        # v1.7: RDS audit dwell + scheduled-sweep change digest
        if "audit_dwell_s" in patch:
            v = float(patch["audit_dwell_s"] or 5.0)
            if not 1.0 <= v <= 30.0:
                raise ValueError("audit dwell must be 1-30 seconds")
            a["audit_dwell_s"] = v
        for k in ("digest_enabled", "digest_email_enabled"):
            if k in patch:
                a[k] = bool(patch[k])
        if "digest_hour" in patch:
            v = int(patch["digest_hour"])
            if not 0 <= v <= 23:
                raise ValueError("digest hour must be 0-23")
            a["digest_hour"] = v
        if "digest_smtp_port" in patch:
            v = int(patch["digest_smtp_port"] or 587)
            if not 1 <= v <= 65535:
                raise ValueError("SMTP port out of range")
            a["digest_smtp_port"] = v
        for k in ("digest_smtp_host", "digest_smtp_user",
                  "digest_smtp_pass", "digest_email_from",
                  "digest_email_to"):
            if k in patch:
                a[k] = str(patch[k] or "").strip()
        with self._lock:
            self.agent = a
            self._save_agent_config()
            interval = float(a.get("sweep_interval_min") or 0)
            if interval <= 0:
                self._sched["next_t"] = None
            else:
                self._sched["next_t"] = time.time() + interval * 60
            # v1.7: re-arm the digest schedule immediately so the UI sees
            # next_run_in_s without waiting for the next scheduler tick
            if a.get("digest_enabled"):
                self._digest_state["next_t"] = digest_mod.next_daily_run(
                    time.time(), max(0, min(23, int(a.get("digest_hour")
                                                   or 9))))
            else:
                self._digest_state["next_t"] = None
        return self.agent_status()

    def _scheduler_loop(self):
        while not self._sched_stop.wait(timeout=5.0):
            try:
                self._scheduler_tick(time.time())
            except Exception as exc:      # never let the agent die silently
                try:
                    with self._lock:
                        self._sched["last_note"] = \
                            f"scheduler error: {exc}"
                except Exception:
                    pass
            try:
                self._digest_tick(time.time())
            except Exception as exc:      # the digest must never kill ticks
                try:
                    self._digest_state["last_note"] = \
                        f"digest error: {exc}"
                except Exception:
                    pass

    def _scheduler_tick(self, now):
        with self._lock:
            a = dict(self.agent)
            interval = float(a.get("sweep_interval_min") or 0)
            scan_running = self._scan["running"]
            sched = self._sched
        if interval <= 0:
            sched["next_t"] = None
            return
        if sched.get("sweeping"):
            # a scheduled sweep is in flight: watch for completion
            if not scan_running:
                sched["sweeping"] = False
                self._after_scheduled_sweep(now)
            return
        if scan_running:
            return          # user-driven scan/sweep: try again next tick
        if sched.get("next_t") is None:
            sched["next_t"] = (sched.get("last_run") or now) \
                + interval * 60
            return
        if now < sched["next_t"]:
            return
        if not a.get("sweep_allow_listeners") and self._sub_count() > 0:
            with self._lock:
                sched["last_note"] = "deferred — a browser tab is listening"
                sched["next_t"] = now + 60
            return
        try:
            res = self.start_sweep(bands=a.get("sweep_bands"),
                                   trigger="scheduled")
            with self._lock:
                sched["sweeping"] = True
                sched["last_run"] = now
                sched["last_note"] = ("scheduled sweep started ("
                                      + str(res["channels"]) + " channels)")
        except Exception as exc:
            with self._lock:
                sched["last_note"] = f"scheduled sweep failed: {exc}"
                sched["next_t"] = now + 300

    # ------------------------------------------------------------------
    # v1.7: scheduled-sweep change digest (daily log/email summary)
    # ------------------------------------------------------------------
    def _digest_tick(self, now):
        """Daily digest scheduler: build yesterday's digest at the
        configured local hour, catch up a missed day after downtime, and
        email it when the agent settings say so. Runs on the scheduler
        thread; never raises."""
        with self._lock:
            a = dict(self.agent)
        st = self._digest_state
        if not a.get("digest_enabled"):
            st["next_t"] = None
            return
        hour = max(0, min(23, int(a.get("digest_hour") or 9)))
        if st.get("next_t") is None:
            st["next_t"] = digest_mod.next_daily_run(now, hour)
        # catch-up: the scheduled hour passed while the app was off and
        # that day still has journal events but no built digest
        for day in (digest_mod.local_day(now - 86400),
                    digest_mod.local_day(now)):
            built_at = digest_mod.day_start_ts(day) + hour * 3600
            if (now >= built_at and not self._digest_store.exists(day)
                    and self._digest.for_day(day)):
                self._run_daily_digest(day, now=now, note="catch-up")
                break
        if now < st.get("next_t", now):
            return
        st["next_t"] = digest_mod.next_daily_run(now + 3600, hour)
        self._run_daily_digest(digest_mod.local_day(now - 86400), now=now)

    def _run_daily_digest(self, day, now=None, note="scheduled",
                          force_email=None):
        """Build (+ optionally email) the digest for one local day.
        force_email: None = follow the agent setting (scheduler path);
        True/False = explicit from the API."""
        with self._lock:
            a = dict(self.agent)
        text = digest_mod.format_digest(day, self._digest.for_day(day))
        path = self._digest_store.save(day, text)
        st = self._digest_state
        st["last_built"] = day
        note_txt = f"{note}: digest {day} written"
        emailed, email_note = False, None
        want_email = (a.get("digest_email_enabled")
                      if force_email is None else force_email)
        if want_email:
            ok, email_note = digest_mod.send_email(
                a, f"RTL-SDR Player digest {day}", text)
            emailed = ok
            note_txt += " · " + email_note
        st["last_note"] = note_txt
        return {"ok": True, "day": day, "text": text, "path": path,
                "emailed": emailed, "email_note": email_note}

    def digest_status(self):
        with self._lock:
            a = dict(self.agent)
            st = dict(self._digest_state)
        enabled = bool(a.get("digest_enabled"))
        return {
            "enabled": enabled,
            "hour": max(0, min(23, int(a.get("digest_hour") or 9))),
            "next_run_in_s": (max(0, round(st["next_t"] - time.time()))
                              if enabled and st.get("next_t") else None),
            "last_built": st.get("last_built"),
            "last_note": st.get("last_note"),
            "email_enabled": bool(a.get("digest_email_enabled")),
            "email_configured": digest_mod.email_configured(a),
            "days": self._digest_store.days()[:14],
        }

    def build_digest(self, day=None, send=False):
        """API-facing digest build: today (or a given YYYY-MM-DD),
        optionally emailed right now regardless of the schedule."""
        day = str(day or digest_mod.local_day())
        try:
            time.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise ValueError("day must be YYYY-MM-DD")
        return self._run_daily_digest(day, note="manual",
                                      force_email=bool(send))

    def get_digest(self, day=None):
        """Return a built digest (building it on the fly when the journal
        has events for that day but no digest was built yet)."""
        day = str(day or digest_mod.local_day())
        try:
            time.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise ValueError("day must be YYYY-MM-DD")
        text = self._digest_store.get(day)
        built = text is not None
        if text is None and self._digest.for_day(day):
            text = digest_mod.format_digest(day, self._digest.for_day(day))
            self._digest_store.save(day, text)
        if text is None:
            text = digest_mod.format_digest(day, [])
        return {"ok": True, "day": day, "text": text, "built": built}

    def _sub_count(self):
        with self._lock:
            return len(self._subs)

    def _after_scheduled_sweep(self, now):
        """Post-sweep hook of the scheduler: auto-fill presets and, when
        configured, let the LLM name the unnamed ones (runs on the
        scheduler thread - never on the capture loop)."""
        with self._lock:
            a = dict(self.agent)
            sched = self._sched
        interval = float(a.get("sweep_interval_min") or 0)
        if interval > 0:
            sched["next_t"] = now + interval * 60
        notes = []
        if a.get("sweep_auto_fill") and (self.sweep_results or []):
            try:
                placed = self.fill_stations_from_sweep(keep_locked=True,
                                                       source="auto-fill")
                notes.append(f"auto-filled {len(placed)} presets")
            except Exception as exc:
                notes.append(f"auto-fill failed: {exc}")
        if (a.get("auto_enrich") and a.get("llm_enabled")
                and a.get("llm_base_url") and a.get("llm_api_key")):
            try:
                res = self.enrich_station_names(a)
                if res.get("ok") and res.get("updated"):
                    notes.append(f"AI named {res['updated']} presets")
            except Exception as exc:
                notes.append(f"AI naming failed: {exc}")
        try:
            self._digest.record("scheduled_notes", notes=notes)
        except Exception:
            pass
        with self._lock:
            sched["last_note"] = "sweep done: " + \
                (", ".join(notes) if notes else
                f"{len(self.sweep_results or [])} stations")

    def enrich_station_names(self, acfg=None):
        """LLM naming for the 20 presets (Agent > 'AI names').
        Updates ai_name/genre per slot; the plain name stays untouched so
        a bad suggestion is always one rename away from reverted."""
        a = acfg or self.agent
        if not a.get("llm_enabled"):
            return {"ok": False, "error":
                    "LLM naming is off — open Agent > LLM settings, "
                    "enter an OpenAI-compatible base URL + API key and "
                    "enable it."}
        if not (a.get("llm_base_url") and a.get("llm_api_key")):
            return {"ok": False, "error":
                    "LLM not configured — set base URL and API key in "
                    "Agent > LLM settings."}
        with self._lock:
            stations = [dict(s) if s else None
                        for s in self.stations]
        updated = agent_mod.enrich_stations(stations, a)
        if not updated:
            return {"ok": True, "updated": 0, "note": "nothing to name"}
        with self._lock:
            for u in updated:
                i = int(u["slot"]) - 1
                if 0 <= i < self.NUM_STATIONS and self.stations[i]:
                    self.stations[i]["ai_name"] = u["name"]
                    if u.get("genre"):
                        self.stations[i]["genre"] = u["genre"]
            old = self.stations
            self._save_stations()
        self._commit_stations(old, "AI naming")
        try:
            self._digest.record("ai_named", updated=len(updated))
        except Exception:
            pass
        return {"ok": True, "updated": len(updated)}

    # ------------------------------------------------------------------
    # v1.6: PI-code online callsign lookup (post-sweep enrichment)
    # ------------------------------------------------------------------
    def _maybe_start_pi_enrich(self):
        """After a sweep, resolve FM PI codes that had no RDS PS name to
        real station identities via the online PI databases. Runs on a
        daemon thread: the capture loop must never block on the network."""
        if not self.agent.get("pi_lookup_enabled", True):
            return
        if self._pi_thread and self._pi_thread.is_alive():
            return
        self._pi_thread = threading.Thread(target=self._pi_enrich_run,
                                           daemon=True, name="pi-lookup")
        self._pi_thread.start()

    def _pi_enrich_run(self):
        """Rename sweep results (and matching unlocked presets) whose only
        name is the 'FM 96.1' fallback and whose PI resolves online."""
        default_re = re.compile(r"^FM \d")
        try:
            targets = [r for r in (self.sweep_results or [])
                       if r.get("pi") and r.get("band") == "wfm"
                       and default_re.match(r.get("name") or "")][:24]
            named = 0
            for r in targets:
                res = agent_mod.pi_lookup(r["pi"], dict(self.agent))
                name = (res or {}).get("name")
                if not name:
                    continue
                name = str(name).strip()[:32]
                if default_re.match(r.get("name") or ""):
                    r["name"] = name
                    named += 1
                with self._lock:
                    old = list(self.stations)
                    touched = False
                    for st in self.stations:
                        if (st and st.get("band") == "wfm"
                                and abs(int(st["freq_hz"])
                                        - int(r["freq_hz"])) < 500
                                and not st.get("locked")
                                and not st.get("ai_name")
                                and default_re.match(st.get("name") or "")):
                            st["name"] = name
                            st["pi"] = st.get("pi") or r["pi"]
                            touched = True
                    if touched:
                        self._save_stations()
                        self._commit_stations(old, "PI lookup")
            if named:
                self._save_sweep_results(self.sweep_meta)
                try:
                    self._digest.record("pi_lookup", named=named)
                except Exception:
                    pass
        except Exception:
            pass  # enrichment is best-effort; never break the receiver

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

    def get_view(self):
        """Prebuilt spectrum-WS payload (capture-thread made, one per
        block). The websocket handler must never touch engine internals
        from the event loop: during fast sweep retunes a loop-side engine
        call + send combination could wedge the transport (v1.6), so the
        handler now only reads this snapshot."""
        return self.view

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
                # v1.7.2: Linux USB diagnostics when the dongle is not in
                # use (sim fallback / not yet opened) - the browser shows
                # the hint in a banner. None in hardware mode.
                "usb_probe": (devmod.probe_linux_usb_cached()
                              if (self.info.get("mode") != "hardware"
                                  and devmod.IS_LINUX) else None),
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
                "stations": self.stations_list(),
                "stations_rev": self._stations_rev,
                "stations_change": self._stations_change,
                "agent": self.agent_status(),
                "digest": self.digest_status(),
                "audit": self.audit_status(),
                "target_buffer_ms": int(self.conf["audio_target_buffer_s"] * 1000),
                "started": self.started,
                "capture": {
                    "usb_overflows": self.stats["usb_overflows"],
                    "short_reads": self.stats["short_reads"],
                    "dsp_ms": round(self.stats["dsp_ms"], 1),
                    "queue_blocks": len(self._raw_q),
                },
            }
            s["tv"] = tv.status()
            return s

    def _build_view(self, psd):
        """One immutable spectrum-WS snapshot, made on the capture thread
        while the block is fresh (lock taken briefly, never held across
        awaits)."""
        return {
            "event": "psd",
            "gen": self.gen,
            "band": self.band,
            "freq_hz": self.freq,
            "demod_mode": self.demod_mode(),
            "bfo_hz": self.bfo_hz,
            "rds": self.current_rds(),
            "scan": self.scan_status(),
            "squelch_db": self.squelch_db,
            "squelch_open": self.squelch_open,
            "ctcss_hz": self.ctcss_hz,
            "ctcss_dev_hz": self._ctcss_dev_hz(),
            "ctcss_open": self._ctcss_open(),
            "chan_db": self.chan_db,
            **psd,
        }

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
        self.view = None
        self.gen += 1

    def _hop_scan(self):
        """Advance the scanner/sweep/audit to the next channel (lock held)."""
        sc = self._scan
        sc["idx"] += 1
        if sc["idx"] >= len(sc["chans"]):
            # list exhausted
            if sc["mode"] == "sweep":
                self._close_sweep(save=True,
                                  restore=sc.get("restore"))
                sc["running"] = False
                sc["state"] = "done"
                return
            if sc["mode"] == "audit":
                self._close_audit(save=True, restore=sc.get("restore"))
                sc["running"] = False
                sc["state"] = "done"
                return
            sc["idx"] = 0          # live scan wraps forever
        band, freq = sc["chans"][sc["idx"]]
        sc["freq_hz"] = freq
        sc["state"] = "scan"
        sc["closed_t"] = None
        sc["hop_t"] = time.monotonic()
        sc["hop_gen"] = self.gen + 1
        sc["cur_db"] = None
        sc["rds_name"] = None
        sc["rds_pi"] = None
        sc["rds_pty"] = None
        self._pending = ("tune", band, freq)

    def _run_scanner(self, now, sq_open):
        """Scanner/sweep state machine; True when audio should be muted."""
        sc = self._scan
        if not sc["running"]:
            return False
        if sc["mode"] == "sweep":
            return self._sweep_step(now)
        if sc["mode"] == "audit":
            return self._audit_step(now)
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

    def _sweep_step(self, now):
        """One processed block in sweep mode (lock held by caller).

        Tracks the peak channel power of the current channel, grabs the
        RDS PS name while dwelling on a hot FM channel, records stations
        above threshold, and hops onward at the configured pace. Just
        before the AM segment, a fast probe pass measures the band's
        noise floor so the AM threshold adapts to conditions (v1.6).
        """
        sc = self._scan
        if sc["idx"] >= len(sc["chans"]):
            return False
        if sc.get("probe") is not None:
            return self._am_probe_step(now)
        band, _freq = sc["chans"][sc["idx"]]
        # v1.6: first AM channel reached -> measure the band noise floor
        # with a quick low-dwell probe pass, then sweep AM adaptively
        if (band == "am" and not sc.get("am_probed")
                and sc.get("am_probe_chans")):
            sc["probe"] = {"chans": sc["am_probe_chans"], "i": 0,
                           "vals": [], "cur": None,
                           "resume_idx": sc["idx"]}
            sc["state"] = "probe"
            pf = sc["probe"]["chans"][0]
            sc["freq_hz"] = pf
            sc["hop_t"] = time.monotonic()
            sc["hop_gen"] = self.gen + 1
            sc["cur_db"] = None
            self._pending = ("tune", "am", pf)
            return True
        # only measure after the hop's tune has actually been applied
        # (gen reaches hop_gen): blocks still in flight from the previous
        # channel would otherwise pollute the first measurement
        if self.gen >= sc["hop_gen"] and self.chan_db is not None:
            if sc["cur_db"] is None or self.chan_db > sc["cur_db"]:
                sc["cur_db"] = float(self.chan_db)
        thr = self._sweep_thr(band)
        hot = (sc["cur_db"] is not None and sc["cur_db"] >= thr)
        if (hot and sc["rds_name"] is None and band == "wfm"):
            snap = self.current_rds()
            if snap and snap.get("ps"):
                sc["rds_name"] = str(snap["ps"]).strip()[:32] or None
                sc["rds_pi"] = snap.get("pi")
                sc["rds_pty"] = snap.get("pty")
        elapsed = now - sc["hop_t"]
        # AM (direct sampling) retunes settle slowly: give each AM channel
        # a longer dwell on top of the sensitivity-shaped threshold
        dwell = sc["dwell"] * (1.8 if band == "am" else 1.0)
        # hold a little longer on a hot FM channel so the PS name arrives,
        # but only when a stereo pilot is present (no pilot = no RDS at all)
        has_pilot = (band == "wfm" and hasattr(self._demod, "pilot")
                     and self._demod.pilot.present)
        waiting_rds = (hot and band == "wfm" and has_pilot
                       and sc["rds_name"] is None
                       and elapsed < dwell + sc["rds_wait"])
        if elapsed < dwell or waiting_rds:
            return True                     # muted while sweeping
        self._sweep_record(band, _freq)
        self._hop_scan()
        return True

    def _sweep_thr(self, band):
        """Per-band sweep threshold: AM uses its measured noise floor +
        the sensitivity margin when the probe pass succeeded."""
        sc = self._scan
        if band == "am" and sc.get("am_thr_db") is not None:
            return sc["am_thr_db"]
        return sc["threshold_db"]

    def _am_probe_step(self, now):
        """One block of the AM noise-floor probe pass (lock held)."""
        sc, pr = self._scan, self._scan["probe"]
        if self.gen >= sc["hop_gen"] and self.chan_db is not None:
            if pr["cur"] is None or self.chan_db > pr["cur"]:
                pr["cur"] = float(self.chan_db)
        elapsed = now - sc["hop_t"]
        # Probe cadence note (v1.6): hopping faster than ~3 retunes/s
        # wedges newly-connected websocket transports (sends suspend and
        # never resume — reproducible with any payload size and both
        # uvicorn WS implementations). 0.35 s/ch matches the proven-safe
        # WFM sweep cadence; the probe costs ~41 s on a full AM sweep.
        if elapsed < max(0.35, sc["dwell"]):
            return True
        if pr["cur"] is not None:
            pr["vals"].append(pr["cur"])
        pr["cur"] = None
        pr["i"] += 1
        if pr["i"] >= len(pr["chans"]):
            vals = sorted(pr["vals"])
            floor = vals[len(vals) // 2] if len(vals) >= 5 else None
            margin = {"low": 12.0, "medium": 6.0, "high": 3.0}.get(
                sc.get("am_sens") or "medium", 6.0)
            sc["am_floor_db"] = (round(floor, 1)
                                 if floor is not None else None)
            sc["am_thr_db"] = (round(floor + margin, 1)
                               if floor is not None else None)
            sc["am_probed"] = True
            sc["probe"] = None
            # resume the main sweep at the AM segment where the probe began
            sc["idx"] = pr["resume_idx"] - 1
            self._hop_scan()
            return True
        _b, freq = pr["chans"][pr["i"]]
        sc["freq_hz"] = freq
        sc["hop_t"] = time.monotonic()
        sc["hop_gen"] = self.gen + 1
        sc["cur_db"] = None
        self._pending = ("tune", "am", freq)
        return True

    def _sweep_record(self, band, freq):
        sc = self._scan
        db = sc.get("cur_db")
        if db is None or db < self._sweep_thr(band):
            return
        rec = {
            "band": band, "freq_hz": int(freq), "db": round(db, 1),
            "name": sc.get("rds_name") or self._default_station_name(band, freq),
            "mode": None,
        }
        if sc.get("rds_pi"):
            rec["pi"] = sc["rds_pi"]
        if sc.get("rds_pty"):
            rec["pty"] = sc["rds_pty"]
        results = sc.setdefault("results", [])
        results.append(rec)
        self._record_hit(freq)

    def _close_sweep(self, save=True, restore=None):
        """Finish a sweep: persist results, optionally restore tuning.
        Called on natural completion, stop, and manual-tune interrupts
        (lock held by caller)."""
        sc = self._scan
        results = sc.get("results") or []
        if results:
            self.sweep_results = results
            self.sweep_meta = {"t": time.time(), "bands": sc.get("bands")}
            self._save_sweep_results({"t": self.sweep_meta["t"],
                                      "bands": sc.get("bands")})
        if restore:
            self._pending = ("tune", restore[0], restore[1])
        if results:
            self._maybe_start_pi_enrich()
        try:
            self._digest.record(
                "sweep", trigger=sc.get("trigger") or "manual",
                bands=sc.get("bands"), found=len(results),
                am_floor_db=sc.get("am_floor_db"),
                seconds=((time.time() - sc["t0"]) if sc.get("t0") else None))
        except Exception:
            pass                      # the digest journal never breaks sweeps

    # ------------------------------------------------------------------
    # v1.7: spectrum-wide RDS audit
    # ------------------------------------------------------------------
    def _audit_step(self, now):
        """One processed block in audit mode (lock held by caller).

        Accumulates the RDS decoder output of the current channel for the
        whole dwell window: PI codes seen (with counts), the best PS state
        reached, programme type, RadioText, clock, pilot presence, peak
        channel power, and the group/error counters of the decoder (their
        deltas give group rate and an approximate block-error ratio).
        """
        sc = self._scan
        if sc["idx"] >= len(sc["chans"]):
            return False
        cur = sc.get("audit_cur")
        if cur is None or cur.get("freq") != sc["freq_hz"]:
            cur = {"freq": sc["freq_hz"], "pis": {}, "ps": None,
                   "ps_missing": 9, "pty": None, "tp": None, "ta": None,
                   "rt": None, "clock": None, "groups0": None,
                   "groups1": 0, "errors1": 0, "db": None, "pilot": False}
            sc["audit_cur"] = cur
        settled = self.gen >= sc["hop_gen"]
        if settled:
            if self.chan_db is not None \
                    and (cur["db"] is None or self.chan_db > cur["db"]):
                cur["db"] = float(self.chan_db)
            snap = self.current_rds()
            if snap:
                cur["pilot"] = bool(cur["pilot"] or snap.get("pilot"))
                if snap.get("pi"):
                    cur["pis"][snap["pi"]] = \
                        cur["pis"].get(snap["pi"], 0) + 1
                if snap.get("groups"):
                    if cur["groups0"] is None:
                        cur["groups0"] = snap["groups"]
                    cur["groups1"] = snap["groups"]
                    cur["errors1"] = snap.get("errors") or 0
                miss = snap.get("ps_missing", 9)
                if snap.get("ps"):
                    cur["ps"] = snap["ps"]
                    cur["ps_missing"] = 0
                elif (snap.get("ps_partial") and miss < cur["ps_missing"]):
                    cur["ps"] = snap["ps_partial"]
                    cur["ps_missing"] = miss
                for k in ("pty", "tp", "ta", "clock"):
                    if cur.get(k) is None and snap.get(k) is not None:
                        cur[k] = snap[k]
                if cur.get("rt") is None and snap.get("radiotext"):
                    cur["rt"] = snap["radiotext"]
        if now - sc["hop_t"] < float(sc["dwell"]):
            return True              # muted while auditing
        self._audit_record(cur)
        self._hop_scan()
        return True

    def _audit_record(self, cur):
        """Finalize one audited channel: quality score + report row."""
        sc = self._scan
        dwell = max(0.5, float(sc["dwell"]))
        groups = max(0, int(cur.get("groups1") or 0)
                     - int(cur.get("groups0") or 0))
        errors = max(0, int(cur.get("errors1") or 0))
        blocks = groups * 4 + errors
        ber = (errors / blocks) if blocks else None
        gps = groups / dwell
        pis = cur.get("pis") or {}
        pi = max(pis, key=pis.get) if pis else None
        pi_total = sum(pis.values())
        pi_stab = (pis[pi] / pi_total) if pi and pi_total else 0.0
        ps = cur.get("ps")
        ps_complete = bool(ps) and cur.get("ps_missing", 9) == 0
        # score: identity (PI) + complete name dominate, then programme
        # type, RadioText, healthy group rate and a clean block stream
        score = 0.0
        if pi:
            score += 30.0 * pi_stab if pi_total > 1 else 30.0
        if ps_complete:
            score += 25.0
        elif ps:
            score += 12.0
        if cur.get("pty"):
            score += 8.0
        if cur.get("rt"):
            score += 4.0
        score += min(15.0, gps / 0.6)   # ~9 groups/s saturates
        if ber is not None:
            score += max(0.0, 18.0 * (1.0 - ber / 0.3))
        score = round(min(100.0, score), 1)
        name = ps
        if not name:
            for r in self.sweep_results or []:
                if (r.get("band") == "wfm"
                        and abs(int(r["freq_hz"]) - cur["freq"]) < 500):
                    name = r.get("name")
                    break
        rec = {
            "band": "wfm", "freq_hz": int(cur["freq"]),
            "name": (name or
                     self._default_station_name("wfm", cur["freq"]))[:32],
            "pi": pi,
            "pi_stability": (round(pi_stab, 2) if pi_total > 1 else None),
            "ps": ps, "ps_complete": ps_complete,
            "pty": cur.get("pty"), "tp": cur.get("tp"),
            "ta": cur.get("ta"), "radiotext": cur.get("rt"),
            "clock": cur.get("clock"), "pilot": bool(cur["pilot"]),
            "db": (round(cur["db"], 1)
                   if cur.get("db") is not None else None),
            "groups": groups, "groups_per_s": round(gps, 2),
            "ber": round(ber, 3) if ber is not None else None,
            "score": score,
        }
        sc.setdefault("results", []).append(rec)

    def _close_audit(self, save=True, restore=None):
        """Finish an audit: persist the report, restore tuning (lock held
        by caller). Called on natural completion, stop and manual-tune
        interrupts; partial reports are kept."""
        sc = self._scan
        results = sc.get("results") or []
        t0 = sc.get("t0")
        if save and results:
            scores = [r["score"] for r in results
                      if isinstance(r.get("score"), (int, float))]
            avg = (round(sum(scores) / len(scores), 1)
                   if scores else None)
            worst = (min(results, key=lambda r: r.get("score", 0))["name"]
                     if results else None)
            meta = {"t": time.time(), "dwell_s": sc.get("dwell"),
                    "channels": len(results), "avg_score": avg,
                    "worst": worst, "results": results}
            self._audit_meta = {"last_t": meta["t"], "count": len(results),
                                "note": (f"avg score {avg}" if avg is not None
                                         else f"{len(results)} channels")}
            self._save_audit(meta)
            try:
                self._digest.record(
                    "audit", channels=len(results), avg_score=avg,
                    worst=worst,
                    seconds=((time.time() - t0) if t0 else None))
            except Exception:
                pass
        sc["audit_cur"] = None
        if restore:
            self._pending = ("tune", restore[0], restore[1])

    def _save_audit(self, meta):
        """Persist an audit report: audit_latest.json + a stamped history
        file, keeping the newest AUDIT_KEEP_COUNT."""
        try:
            adir = os.path.join(self._state_dir, "audits")
            os.makedirs(adir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.localtime(meta["t"]))
            for name in (f"{stamp}.json", "audit_latest.json"):
                tmp = os.path.join(adir, name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=1)
                os.replace(tmp, os.path.join(adir, name))
            olds = sorted(f for f in os.listdir(adir)
                          if f.endswith(".json") and f != "audit_latest.json")
            for f in olds[:-cfg.AUDIT_KEEP_COUNT]:
                os.remove(os.path.join(adir, f))
        except OSError:
            pass                    # reports are a convenience; never break
                                          # the capture loop

    def audit_report(self):
        """The latest audit report (dict) or None."""
        path = os.path.join(self._state_dir, "audits", "audit_latest.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) \
                    and isinstance(data.get("results"), list):
                return data
        except (OSError, ValueError):
            pass
        return None

    def audit_status(self):
        with self._lock:
            sc = self._scan
            running = bool(sc["running"] and sc["mode"] == "audit")
            return {
                "running": running,
                "idx": sc["idx"] if running else 0,
                "count": len(sc["chans"]) if running else 0,
                "dwell_s": (float(sc.get("dwell") or 0) if running
                            else None),
                "results": len(sc.get("results") or []),
                "last_t": self._audit_meta.get("last_t"),
                "last_count": self._audit_meta.get("count"),
                "last_note": self._audit_meta.get("note"),
            }

    def _reader_loop(self):
        while not self._stop.is_set():
            self._reader_step()

    def _reader_step(self):
        """One USB ingestion step (kept single-step so tests can drive it).

        Blocks on device.read_iq() like before, but lives on its own thread:
        librtlsdr's transfer ring stays continuously fed while the DSP thread
        works, so Python processing time no longer subtracts from realtime.
        Short reads are accumulated until a full block exists instead of
        being thrown away (the old loop discarded up to half a block, which
        audibly chopped the audio on real hardware).
        """
        if self._pause_req.is_set():
            # retune in progress: park before touching the device again
            self._parked.set()
            self._resume.wait(timeout=0.25)   # poll so _stop still works
            self._resume.clear()
            return
        try:
            t_read0 = time.monotonic()
            raw = self.device.read_iq(dsp.BLOCK)
        except Exception as exc:
            self.info["error"] = f"capture failed: {exc}"
            time.sleep(0.3)
            return
        if self._pause_req.is_set():
            return   # block captured at the old rate during a retune: discard
        if not len(raw):
            return   # device hiccup: nothing came out this time
        gen_t = time.monotonic() - t_read0
        if len(raw) < dsp.BLOCK:
            self.stats["short_reads"] += 1
            raw = raw if self._residue is None else \
                np.concatenate((self._residue, raw))
            self._residue = None
            if len(raw) < dsp.BLOCK:
                self._residue = raw
                return
        self._put_block(raw[:dsp.BLOCK])
        if len(raw) > dsp.BLOCK:            # keep any surplus for next block
            self._residue = raw[dsp.BLOCK:]
        if not getattr(self.device, "is_real", True):
            # simulator produces instantly: pace the reader to realtime so
            # the queue/capacity stats stay meaningful and we don't spin.
            # The generation time is subtracted from the budget so even the
            # heavier synthetic stations (AM direct sampling) land on the
            # realtime schedule instead of drifting into starvation.
            budget = getattr(self, "_block_seconds", 0.109)
            time.sleep(max(0.0, budget - gen_t))

    def _put_block(self, raw):
        if len(self._raw_q) >= self._raw_max:
            # brief yield first: lets a merely-busy DSP thread catch up
            # without dropping (matters in sim, which produces instantly)
            time.sleep(0.005)
            if len(self._raw_q) < self._raw_max:
                self._raw_q.append(raw)
                return
            # DSP is genuinely falling behind: drop the oldest block so
            # latency stays bounded (counted in /api/status capture stats)
            try:
                self._raw_q.popleft()
                self.stats["usb_overflows"] += 1
            except IndexError:
                pass
        self._raw_q.append(raw)

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
                        # sample rate / center freq change: park the USB
                        # reader so it never reads across the reconfig
                        self._pause_req.set()
                        self._parked.clear()   # consume any stale flag
                        self._parked.wait(timeout=1.5)
                        try:
                            self._apply_tune(pending[1], pending[2])
                            self._raw_q.clear()
                            self._residue = None
                        finally:
                            self._pause_req.clear()
                            self._resume.set()
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

            # ---- process (blocks arrive from the sdr-usb thread) ----
            if not self._raw_q:
                time.sleep(0.004)
                continue
            raw = self._raw_q.popleft()
            t_dsp = time.monotonic()
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
                # squelch gates audio on any band while enabled and closed.
                # Gate edges get a 4 ms ramp (v1.7.1): a hard block-boundary
                # cut clicks audibly, most noticeably on NBFM/AM squelch
                # tails and scanner hops.
                if self.squelch_db is not None and not eff_open:
                    muted = True
                if muted:
                    if self._sq_was_muted:
                        pcm = np.zeros_like(pcm)
                    else:
                        pcm = pcm.copy()
                        nfade = min(192, len(pcm))      # 4 ms @ 48 kHz
                        ramp = np.linspace(1.0, 0.0, nfade) ** 2
                        pcm[:nfade] = np.rint(pcm[:nfade] * ramp)
                        pcm[nfade:] = 0
                elif self._sq_was_muted:
                    pcm = pcm.copy()
                    nfade = min(192, len(pcm))
                    ramp = np.linspace(0.0, 1.0, nfade) ** 2
                    pcm[:nfade] = np.rint(pcm[:nfade] * ramp)
                self._sq_was_muted = bool(muted)

                for q in list(self._subs.values()):
                    q.append(pcm)
                self.psd = psd_comp.process(raw, self._fs, self._center,
                                            self.freq)
                self.view = self._build_view(self.psd)
                self.stats["dsp_ms"] = (
                    0.9 * self.stats["dsp_ms"]
                    + 0.1 * (time.monotonic() - t_dsp) * 1000.0)
            except Exception as exc:
                self.info["error"] = f"dsp error: {exc}"
                time.sleep(0.1)

            # ---- pacing (keeps sim at realtime; on hardware the USB thread
            # paces the capture side, this just avoids spinning) ----
            elapsed = time.monotonic() - t0
            if elapsed < block_dur:
                time.sleep(block_dur - elapsed)
