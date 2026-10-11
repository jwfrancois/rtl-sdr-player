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

        # v1.4: the 20 named station presets ("STATIONS" panel). Each slot
        # is {slot, name, band, freq_hz, mode} or None when empty.
        self._st_path = os.path.join(cfg.ROOT,
                                     conf.get("stations_file",
                                              "stations.json"))
        self.stations = self._load_stations()

        # v1.4: last all-band sweep results (persisted, feeds "fill presets")
        self._sweep_path = os.path.join(cfg.ROOT,
                                        conf.get("sweep_results_file",
                                                 "sweep_results.json"))
        self.sweep_results = self._load_sweep_results()

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
            "restore": None, "t0": None,
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
        self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                        name="sdr-usb")
        self._thread.start()
        self._reader.start()
        self.started = True

    def close(self):
        self._stop.set()
        self._pause_req.clear()
        self._resume.set()          # unblock a parked reader
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._reader:
            self._reader.join(timeout=2.0)
        if self.device:
            self.device.close()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def tune(self, band=None, freq_hz=None, mode=None):
        with self._lock:
            if self._scan["running"]:
                # manual tune wins over scanner/sweep; a sweep keeps the
                # partial results but does not yank the tuner back
                self._scan["running"] = False
                self._scan["state"] = "idle"
                if self._scan["mode"] == "sweep":
                    self._close_sweep(save=True, restore=None)
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
                    from_hz=None, to_hz=None):
        """All-band measurement sweep (v1.4).

        Tunes across every channel of the selected bands, records the peak
        channel power and (on FM) the RDS station name of each hot channel,
        then leaves a sorted result list behind for the STATIONS panel's
        "fill presets" action. Audio is muted while sweeping; the previous
        tuning is restored at the end (or on stop / manual tune).
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
                "results": [],
            })
        return {"ok": True, "bands": ordered, "channels": len(chans),
                "threshold_db": thr, "dwell_s": dwell}

    def _begin_scan(self, mode, chans, list_name, label, order):
        """Common scan/sweep setup (lock held by caller)."""
        self._scan.update({
            "running": True, "list": list_name, "mode": mode, "label": label,
            "idx": 0, "state": "scan", "freq_hz": chans[0][1],
            "last_active_hz": None, "closed_t": None,
            "hop_t": time.monotonic(), "hop_gen": self.gen + 1,
            "freqs": [f for _, f in chans], "chans": list(chans),
            "order": order, "cur_db": None, "rds_name": None,
        })
        band0, freq0 = chans[0]
        self._pending = ("tune", band0, freq0)

    def stop_scan(self):
        with self._lock:
            if not self._scan["running"]:
                self._scan["state"] = "idle"
                return
            was_sweep = self._scan["mode"] == "sweep"
            restore = self._scan.get("restore") if was_sweep else None
            self._scan["running"] = False
            self._scan["state"] = "idle"
            if was_sweep:
                self._close_sweep(save=True, restore=restore)

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
                #    its name (right-click save after a sweep)
                if not name:
                    for r in self.sweep_results or []:
                        if (r.get("band") == band
                                and r.get("freq_hz") == freq_hz
                                and r.get("name")):
                            name = r["name"]
                            break
                name = name or self._default_station_name(band, freq_hz)
            st = {"slot": slot, "name": str(name).strip()[:32] or "?",
                  "band": band, "freq_hz": freq_hz, "mode": mode,
                  "added": round(time.time(), 1)}
            self.stations[slot - 1] = st
            self._save_stations()
            return st

    def clear_station(self, slot):
        if not 1 <= int(slot) <= self.NUM_STATIONS:
            raise ValueError(f"slot must be 1..{self.NUM_STATIONS}")
        with self._lock:
            gone = self.stations[int(slot) - 1]
            self.stations[int(slot) - 1] = None
            self._save_stations()
        return gone is not None

    def clear_stations(self):
        with self._lock:
            self.stations = [None] * self.NUM_STATIONS
            self._save_stations()
        return {"ok": True}

    def fill_stations_from_sweep(self, count=None):
        """Populate the 20 slots from the last sweep results.

        Strongest stations first, one per channel (adjacent-channel
        shoulders are merged so one broadcast does not eat two slots).
        Existing slot contents are overwritten.
        """
        results = [r for r in (self.sweep_results or [])
                   if r.get("db") is not None]
        if not results:
            raise ValueError("no sweep results yet — run a sweep first")
        count = int(count or self.NUM_STATIONS)
        count = max(1, min(self.NUM_STATIONS, count))
        picks = self._dedupe_results(results)[:count]
        with self._lock:
            self.stations = [None] * self.NUM_STATIONS
            for i, r in enumerate(picks):
                band = r["band"]
                self.stations[i] = {
                    "slot": i + 1,
                    "name": (r.get("name")
                             or self._default_station_name(band,
                                                           r["freq_hz"]))[:32],
                    "band": band,
                    "freq_hz": int(r["freq_hz"]),
                    "mode": r.get("mode"),
                    "added": round(time.time(), 1),
                }
            self._save_stations()
        return picks

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
                "stations": self.stations_list(),
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
        """Advance the scanner/sweep to the next channel (lock held)."""
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
            sc["idx"] = 0          # live scan wraps forever
        band, freq = sc["chans"][sc["idx"]]
        sc["freq_hz"] = freq
        sc["state"] = "scan"
        sc["closed_t"] = None
        sc["hop_t"] = time.monotonic()
        sc["hop_gen"] = self.gen + 1
        sc["cur_db"] = None
        sc["rds_name"] = None
        self._pending = ("tune", band, freq)

    def _run_scanner(self, now, sq_open):
        """Scanner/sweep state machine; True when audio should be muted."""
        sc = self._scan
        if not sc["running"]:
            return False
        if sc["mode"] == "sweep":
            return self._sweep_step(now)
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
        above threshold, and hops onward at the configured pace.
        """
        sc = self._scan
        if sc["idx"] >= len(sc["chans"]):
            return False
        band, _freq = sc["chans"][sc["idx"]]
        # only measure after the hop's tune has actually been applied
        # (gen reaches hop_gen): blocks still in flight from the previous
        # channel would otherwise pollute the first measurement
        if self.gen >= sc["hop_gen"] and self.chan_db is not None:
            if sc["cur_db"] is None or self.chan_db > sc["cur_db"]:
                sc["cur_db"] = float(self.chan_db)
        hot = (sc["cur_db"] is not None
               and sc["cur_db"] >= sc["threshold_db"])
        if (hot and sc["rds_name"] is None and band == "wfm"):
            snap = self.current_rds()
            if snap and snap.get("ps"):
                sc["rds_name"] = str(snap["ps"]).strip()[:32] or None
        elapsed = now - sc["hop_t"]
        # hold a little longer on a hot FM channel so the PS name arrives,
        # but only when a stereo pilot is present (no pilot = no RDS at all)
        has_pilot = (band == "wfm" and hasattr(self._demod, "pilot")
                     and self._demod.pilot.present)
        waiting_rds = (hot and band == "wfm" and has_pilot
                       and sc["rds_name"] is None
                       and elapsed < sc["dwell"] + sc["rds_wait"])
        if elapsed < sc["dwell"] or waiting_rds:
            return True                     # muted while sweeping
        self._sweep_record(band, _freq)
        self._hop_scan()
        return True

    def _sweep_record(self, band, freq):
        sc = self._scan
        db = sc.get("cur_db")
        if db is None or db < sc["threshold_db"]:
            return
        rec = {
            "band": band, "freq_hz": int(freq), "db": round(db, 1),
            "name": sc.get("rds_name") or self._default_station_name(band, freq),
            "mode": None,
        }
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
            self._save_sweep_results({"t": sc.get("t0") or time.time(),
                                      "bands": sc.get("bands")})
        if restore:
            self._pending = ("tune", restore[0], restore[1])

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
            raw = self.device.read_iq(dsp.BLOCK)
        except Exception as exc:
            self.info["error"] = f"capture failed: {exc}"
            time.sleep(0.3)
            return
        if self._pause_req.is_set():
            return   # block captured at the old rate during a retune: discard
        if not len(raw):
            return   # device hiccup: nothing came out this time
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
            # the queue/capacity stats stay meaningful and we don't spin
            time.sleep(getattr(self, "_block_seconds", 0.109))

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
                # squelch gates audio on any band while enabled and closed
                if self.squelch_db is not None and not eff_open:
                    muted = True
                if muted:
                    pcm = np.zeros_like(pcm)

                for q in list(self._subs.values()):
                    q.append(pcm)
                self.psd = psd_comp.process(raw, self._fs, self._center,
                                            self.freq)
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
