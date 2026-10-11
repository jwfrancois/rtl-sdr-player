"""TV support: capability detection + DVB-T playback (Linux) + region guide.

The RTL2832U contains a hardware DVB-T demodulator. Using it means leaving
"SDR mode": on Linux the kernel driver dvb_usb_rtl28xxu exposes /dev/dvb/adapter0
and standard tools (dvbv5-zap + ffmpeg) can stream TV into the browser. On
Windows, DVB-T mode requires Realtek's BDA driver, which replaces the WinUSB
SDR driver — both modes cannot run side by side, so on Windows we show an
honest capability report and guide instead of a broken player.
"""
import glob
import os
import platform
import shutil
import subprocess
import threading
import time

CHANNEL_CONF_CANDIDATES = [
    os.path.expanduser("~/.config/dvb/channels.conf"),
    os.path.expanduser("~/.tzap/channels.conf"),
    "/etc/dvb/channels.conf",
]

REQUIRED_TOOLS = ("dvbv5-zap", "dvbv5-scan", "ffmpeg")


def _windows_guide():
    return [
        "TV through an RTL-SDR works by switching the dongle into its built-in "
        "DVB-T demodulator mode. That mode is only practical on Linux.",
        "If you are in a DVB-T country (Europe, Asia-Pacific, Africa): the same "
        "app auto-enables the TV panel on Linux. Boot a Linux live USB (or dual-boot), "
        "run this app there, then run 'sudo apt install dvb-tools ffmpeg' and the "
        "TV tab will offer channel scanning and playback.",
        "If you are in North America (US/Canada/Mexico): over-the-air TV is ATSC "
        "(8-VSB), which the RTL2832U hardware cannot decode at all — no driver or "
        "software can change this. The dongle instead shines at FM/AM radio "
        "(built in), NOAA weather radio, airband and ADS-B.",
        "On Windows specifically, DVB-T would require Realtek's BDA driver, which "
        "replaces the WinUSB SDR driver — you would have to re-run Zadig every time "
        "you switch between radio and TV. We therefore keep the SDR driver installed.",
    ]


def _linux_guide():
    return [
        "Install tools: sudo apt install dvb-tools ffmpeg (Debian/Ubuntu) or "
        "dnf install dvb-tools ffmpeg (Fedora).",
        "Stop this app's SDR mode, then load the DVB driver: "
        "sudo modprobe dvb_usb_rtl28xxu",
        "Run a channel scan from the TV tab (takes 2-5 minutes) — results are "
        "saved to ~/.config/dvb/channels.conf.",
        "Pick a channel: the app tunes it with dvbv5-zap and streams MPEG-TS "
        "through this page.",
        "To go back to radio: sudo modprobe -r dvb_usb_rtl28xxu and restart the app.",
    ]


def status():
    system = platform.system()
    if system != "Linux":
        return {
            "available": False,
            "platform": system,
            "adapter": None,
            "tools": {},
            "scanning": False,
            "reason": ("DVB-T playback needs the Linux kernel DVB stack; on "
                       "Windows the dongle stays in SDR (WinUSB) mode."),
            "guide": _windows_guide(),
        }

    adapters = sorted(glob.glob("/dev/dvb/adapter*/frontend0"))
    tools = {t: bool(shutil.which(t)) for t in REQUIRED_TOOLS}
    missing_tools = [t for t, ok in tools.items() if not ok]
    if not adapters:
        reason = ("No DVB adapter found. The dongle is in SDR mode "
                  "(normal for radio). To enable TV: sudo modprobe dvb_usb_rtl28xxu "
                  "then restart the app.")
    elif missing_tools:
        reason = f"Missing tools: {', '.join(missing_tools)}. " + _linux_guide()[0]
    else:
        reason = None
    return {
        "available": bool(adapters) and not missing_tools,
        "platform": system,
        "adapter": adapters[0] if adapters else None,
        "tools": tools,
        "scanning": _scan_state["running"],
        "reason": reason,
        "guide": _linux_guide(),
    }


# ----------------------------------------------------------------------
# Channel scan (Linux, dvbv5-tools)
# ----------------------------------------------------------------------
_scan_state = {"running": False, "message": ""}


def scan_channels(country_code=""):
    """Blocking scan via dvbv5-scan using a generated initial-tuning file.

    dvbv5-scan needs an initial transponder list; without one we scan the
    classic VHF/UHF grid every 8 MHz/7 MHz, which works in most DVB-T regions.
    Runs in a background thread when called through the API.
    """

    def _work():
        _scan_state["running"] = True
        _scan_state["message"] = "Scanning UHF/VHF grid (takes a few minutes)..."
        try:
            conf_out = CHANNEL_CONF_CANDIDATES[0]
            os.makedirs(os.path.dirname(conf_out), exist_ok=True)
            initial = "/tmp/dvbt-initial"
            with open(initial, "w") as fh:
                for ch in range(5, 14):   # VHF: 174-230 MHz (7 MHz grid)
                    fh.write(f"T {174e6 + (ch - 5) * 7e6:.0f} 8MHz AUTO AUTO AUTO "
                             "AUTO AUTO AUTO AUTO AUTO AUTO\n")
                for ch in range(21, 70):  # UHF: 470-694 MHz (8 MHz grid)
                    fh.write(f"T {470e6 + (ch - 21) * 8e6:.0f} 8MHz AUTO AUTO AUTO "
                             "AUTO AUTO AUTO AUTO AUTO AUTO\n")
            with open(conf_out, "w") as out:
                subprocess.run(
                    ["dvbv5-scan", "-a", "0", "-o", "zap", initial],
                    stdout=out, stderr=subprocess.DEVNULL, timeout=600,
                )
            _scan_state["message"] = "Scan finished. Reload the channel list."
        except FileNotFoundError:
            _scan_state["message"] = "dvbv5-scan not installed (" + _linux_guide()[0] + ")"
        except subprocess.TimeoutExpired:
            _scan_state["message"] = "Scan timed out after 10 minutes."
        except Exception as exc:
            _scan_state["message"] = f"Scan failed: {exc}"
        finally:
            _scan_state["running"] = False

    threading.Thread(target=_work, daemon=True).start()


def scan_message():
    return dict(_scan_state)


# ----------------------------------------------------------------------
# Channel list + playback
# ----------------------------------------------------------------------
def find_channels_conf():
    for path in CHANNEL_CONF_CANDIDATES:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def list_channels():
    conf = find_channels_conf()
    if not conf:
        return []
    chans = []
    with open(conf, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # zap format: name:freq:inv:sr:vpid:apid...
            parts = line.split(":")
            if len(parts) >= 5:
                chans.append({"name": parts[0], "freq": parts[1]})
    return chans


class DvbStream:
    """Tunes one channel with dvbv5-zap and pipes the TS through ffmpeg."""

    def __init__(self):
        self.zap = None
        self.ffmpeg = None
        self.lock = threading.Lock()

    def start(self, channel_name):
        with self.lock:
            self.stop()
            conf = find_channels_conf()
            if not conf:
                raise RuntimeError("No channels.conf found — run a scan first.")
            self.zap = subprocess.Popen(
                ["dvbv5-zap", "-c", conf, "-r", channel_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)  # let the frontend lock
            self.ffmpeg = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-i", "/dev/dvb/adapter0/dvr0",
                 "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-c:a", "aac", "-f", "mpegts", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, amount=32768):
        if self.ffmpeg and self.ffmpeg.stdout:
            return self.ffmpeg.stdout.read(amount)
        return b""

    def stop(self):
        for proc in (self.ffmpeg, self.zap):
            if proc and proc.poll() is None:
                proc.terminate()
        self.ffmpeg = self.zap = None


DVBS = DvbStream()
