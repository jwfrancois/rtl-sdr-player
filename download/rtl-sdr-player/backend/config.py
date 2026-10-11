"""Central configuration for the RTL-SDR Player.

User-tunable values live in config.json next to app.py; this module
defines defaults, loads that file, and exposes band/preset/scan tables.
"""
import json
import os

APP_NAME = "RTL-SDR Player"
VERSION = "1.1.0"

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
    "scan_dwell_s": 0.45,          # time spent per channel while scanning
    "scan_release_s": 2.5,         # squelch-closed time before hopping on
}

# Band tables: frequency limits + tuning step per band. The demodulator is
# chosen from the band: wfm -> wideband FM, wx -> narrowband FM,
# am -> AM envelope (direct sampling), air -> AM envelope (offset tuned).
BANDS = {
    "wfm": {
        "label": "FM",
        "lo_hz": 87_500_000,
        "hi_hz": 108_000_000,
        "step_hz": 100_000,
        "desc": "Broadcast FM (wideband FM + RDS)",
        "presets": [],
    },
    "am": {
        "label": "AM",
        "lo_hz": 530_000,
        "hi_hz": 1_700_000,
        "step_hz": 10_000,
        "desc": "Broadcast AM 530-1700 kHz (direct-sampling mode)",
        "presets": [],
    },
    "wx": {
        "label": "WX",
        "lo_hz": 162_400_000,
        "hi_hz": 162_550_000,
        "step_hz": 25_000,
        "desc": "NOAA Weather Radio (narrowband FM, 5 kHz dev)",
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
        "desc": "US airband 118-137 MHz (AM, 25 kHz channels)",
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
}


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
    return cfg


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
