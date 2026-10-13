"""Central configuration for the RTL-SDR Player.

User-tunable values live in config.json next to app.py; this module
defines defaults, loads that file, and exposes band/preset/scan tables.
"""
import json
import os

APP_NAME = "RTL-SDR Player"
VERSION = "1.7.7"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULTS = {
    "port": 8515,
    "region": "US",                # pins RBDS PTY names + 75 us de-emphasis
    "de_emphasis_us": 75,          # 75 us = Americas/Korea, 50 us = EU/Asia
    "fm_sample_rate": 2400000,     # capture rate for VHF bands + spectrum
    "am_sample_rate": 2400000,     # capture rate for AM band (direct sampling)
    "default_band": "wfm",
    "default_freq_hz": 98300000,
    "audio_target_buffer_s": 2.5,  # client-side jitter buffer target
    "sim_fallback": True,          # run in simulation mode when no dongle
    "lo_offset_hz": 250000,        # offset tuning: dodges the DC spike
    "squelch_default_db": -30,     # audio RMS gate used by the scanner
    "squelch_hysteresis_db": 4.0,  # close threshold sits this far below open
    "squelch_auto_bands": {"wx": True, "air": True},
    # ^ NBFM/AM comm bands default to squelch ON (until the user takes over)
    "scan_dwell_s": 0.45,          # time spent per channel while scanning
    "scan_release_s": 2.5,         # squelch-closed time before hopping on
    "sweep_dwell_s": 0.35,         # time spent per channel in the all-band sweep
    "sweep_rds_wait_s": 4.5,       # extra dwell on hot FM channels for RDS PS
    "scan_memory_file": "scan_memory.json",
    "bookmarks_file": "bookmarks.json",
    "stations_file": "stations.json",       # the 20 named station presets
    "sweep_results_file": "sweep_results.json",
    "agent_config_file": "agent_config.json",  # v1.5 agent/scheduler/LLM
    # v1.7: RDS audit dwell per FM channel (spectrum-wide RDS quality audit)
    "audit_dwell_s": 5.0,
    # v1.7: relocate ALL runtime state (stations.json, agent_config.json,
    # pi_cache.json, state/ digest+audit artifacts...) to one directory.
    # Empty = next to app.py (Windows default). The Docker image sets the
    # RTLSDR_DATA_DIR env var so a single volume persists everything.
    "data_dir": "",
}

# Standard EIA CTCSS (PL) tones in Hz — the sub-audible squelch tones used
# on NBFM channels (NOAA-adjacent services, GMRS/MURS/ham repeaters).
CTCSS_TONES = [
    67.0, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9, 186.2,
    192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
]

# Band tables: frequency limits + tuning step per band. The demodulator is
# chosen from the band, with an optional per-band mode override:
#   wfm -> wideband FM            wx -> narrowband FM
#   am  -> AM envelope | USB/LSB  air -> AM envelope | USB/LSB
BANDS = {
    "wfm": {
        "label": "FM",
        "lo_hz": 87_500_000,
        "hi_hz": 108_000_000,
        "step_hz": 100_000,
        "desc": "Broadcast FM (wideband FM + RDS)",
        "modes": ["wfm"],
        "presets": [],
    },
    "am": {
        "label": "AM",
        "lo_hz": 530_000,
        "hi_hz": 1_700_000,
        "step_hz": 10_000,
        "desc": "Broadcast AM 530-1700 kHz + shortwave-style SSB (direct sampling)",
        "modes": ["am", "usb", "lsb"],
        "presets": [],
    },
    "wx": {
        "label": "WX",
        "lo_hz": 162_400_000,
        "hi_hz": 162_550_000,
        "step_hz": 25_000,
        "desc": "NOAA Weather Radio (narrowband FM, 5 kHz dev)",
        "modes": ["nbfm"],
        "presets": [
            {"hz": 162550000, "label": "WX1"},
            {"hz": 162400000, "label": "WX2"},
            {"hz": 162475000, "label": "WX3"},
            {"hz": 162425000, "label": "WX4"},
            {"hz": 162450000, "label": "WX5"},
            {"hz": 162500000, "label": "WX6"},
            {"hz": 162525000, "label": "WX7"},
        ],
    },
    "air": {
        "label": "AIR",
        "lo_hz": 118_000_000,
        "hi_hz": 136_975_000,
        "step_hz": 25_000,
        "desc": "US airband 118-137 MHz (AM, 25 kHz ch, SSB for mil/utility)",
        "modes": ["am", "usb", "lsb"],
        "presets": [
            {"hz": 121500000, "label": "121.500 GUARD"},
            {"hz": 122000000, "label": "122.000 FSS"},
            {"hz": 122750000, "label": "122.750 AIR-AIR"},
            {"hz": 122800000, "label": "122.800 CTAF"},
            {"hz": 122850000, "label": "122.850 AIR-AIR"},
            {"hz": 122950000, "label": "122.950 UNICOM"},
            {"hz": 122975000, "label": "122.975 CTAF"},
            {"hz": 123000000, "label": "123.000 UNICOM"},
            {"hz": 123025000, "label": "123.025 HELI"},
            {"hz": 123100000, "label": "123.100 PARKS"},
        ],
    },
}

# Frequencies (Hz) of the NOAA Weather Radio network channels
NOAA_CHANNELS = [
    162400000, 162425000, 162450000,
    162475000, 162500000, 162525000, 162550000,
]


def _air_band_channels():
    """Full 118.000-136.975 MHz sweep in 25 kHz steps (760 channels)."""
    return list(range(118_000_000, 136_975_000 + 1, 25_000))


def _band_channels(band):
    """Full band sweep at the band's tuning step (for the band scanner)."""
    b = BANDS[band]
    return list(range(b["lo_hz"], b["hi_hz"] + 1, b["step_hz"]))


# Scanner channel lists
SCAN_LISTS = {
    "noaa": {
        "label": "NOAA weather radio (7 channels)",
        "band": "wx",
        "freqs": list(NOAA_CHANNELS),
    },
    "air_key": {
        "label": "Key US airband channels",
        "band": "air",
        "freqs": [p["hz"] for p in BANDS["air"]["presets"]],
    },
    "air_band": {
        "label": "Full airband sweep 118-137 MHz",
        "band": "air",
        "freqs": _air_band_channels(),
    },
    "fm_band": {
        "label": "Full FM broadcast sweep 87.5-108 MHz",
        "band": "wfm",
        "freqs": _band_channels("wfm"),
    },
    "am_band": {
        "label": "Full AM sweep 530-1700 kHz",
        "band": "am",
        "freqs": _band_channels("am"),
    },
}

# Standard US NOAA Weather Radio channel names (162.550 = WX1 ... )
WX_NAMES = {
    162_550_000: "WX1", 162_400_000: "WX2", 162_475_000: "WX3",
    162_425_000: "WX4", 162_450_000: "WX5", 162_500_000: "WX6",
    162_525_000: "WX7",
}

# Sweep order used by the all-bands sweep: fast, high-value bands first so
# the 20 station presets fill up quickly even on a partial sweep.
SWEEP_BAND_ORDER = ["wfm", "am", "wx", "air"]

# v1.7 digest: journal retention (days) and where artifacts live relative
# to the data root (data_root(config)).
DIGEST_KEEP_DAYS = 8
AUDIT_KEEP_COUNT = 10


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except (OSError, ValueError):
            pass  # corrupt config file -> fall back to defaults
    # container/server override: keep all runtime state in one volume
    env_dir = os.environ.get("RTLSDR_DATA_DIR") or ""
    if env_dir:
        cfg["data_dir"] = env_dir
    return cfg


def data_root(conf=None):
    """Directory that holds every runtime artifact (config file names
    resolve here). conf may be a loaded config; defaults to ROOT."""
    base = (conf or {}).get("data_dir") if conf else None
    return base or ROOT


def clamp_to_band(freq_hz, band):
    b = BANDS[band]
    return max(b["lo_hz"], min(b["hi_hz"], int(freq_hz)))


def band_default_freq(band, default_band, default_freq_hz):
    """A sensible starting frequency when switching bands."""
    if band == default_band:
        return default_freq_hz
    return {
        "wfm": 98_300_000,
        "am": 990_000,
        "wx": 162_550_000,
        "air": 121_500_000,
    }.get(band, 98_300_000)
