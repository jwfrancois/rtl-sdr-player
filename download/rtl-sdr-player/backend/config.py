"""Central configuration for the RTL-SDR Player.

User-tunable values live in config.json next to app.py; this module
defines defaults, loads that file, and exposes band tables.
"""
import json
import os

APP_NAME = "RTL-SDR Player"
VERSION = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULTS = {
    "port": 8515,
    "de_emphasis_us": 75,          # 75 us = Americas/Korea, 50 us = most of Europe/Asia
    "fm_sample_rate": 2400000,     # capture rate for FM band + spectrum
    "am_sample_rate": 2400000,     # capture rate for AM band (direct sampling)
    "default_band": "wfm",
    "default_freq_hz": 98300000,
    "audio_target_buffer_s": 2.5,  # client-side jitter buffer target ("smooth" preset)
    "sim_fallback": True,          # run in simulation mode when no dongle is present
    "lo_offset_hz": 250000,        # offset tuning: keeps the DC spike away from the station
}

# Band tables: frequency limits + tuning step per band
BANDS = {
    "wfm": {
        "label": "FM",
        "lo_hz": 87_500_000,
        "hi_hz": 108_000_000,
        "step_hz": 100_000,
        "desc": "Broadcast FM (wideband FM demodulation)",
    },
    "am": {
        "label": "AM",
        "lo_hz": 530_000,
        "hi_hz": 1_700_000,
        "step_hz": 10_000,
        "desc": "Broadcast AM 530-1700 kHz (direct-sampling mode)",
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
