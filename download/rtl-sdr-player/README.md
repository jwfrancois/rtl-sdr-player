# RTL-SDR Player

A local web receiver for the **RTL-SDR Blog v3** (RTL2832U + R820T2): play
over-the-air **radio (FM + AM)** and **DVB-T TV** from your dongle, with a live
spectrum analyzer and waterfall, right in your browser.

```
Dongle → [this app] → http://localhost:8515
             │
             ├── FM band 87.5–108 MHz   wideband FM demod, offset-tuned
             ├── AM band 530–1700 kHz   direct-sampling (Q) mode (v3 feature)
             ├── Live spectrum + waterfall with click-to-tune
             └── TV tab                 DVB-T capability detection + guide
                                        (Linux: full channel scan & playback)
```

---

## Setup (Windows 10 / 11)

**1. Install Python** — 3.10 or newer from <https://www.python.org/downloads/>
(tick *Add python.exe to PATH*).

**2. Bind the WinUSB driver with Zadig** (one-time, ~5 seconds):

1. Download Zadig: <https://zadig.akeo.ie>
2. Plug in the dongle, run Zadig
3. Options → *List All Devices* → select **Bulk-In, Interface (Interface 0)**
   (if you see two entries, pick the one that is **not** "Interface 1")
4. Target driver should read **WinUSB** → click *Replace Driver*

> This replaces Windows' default DVB-T driver. That is exactly what we want:
> SDR software talks to the stick through WinUSB. (To use the stick as a
> DVB-T TV receiver again you would re-run Zadig and pick the original
> driver — see the TV section below.)

**3. Get the native DLLs** — the app needs `rtlsdr.dll` and `libusb-1.0.dll`
in the app folder:

- Download: <https://github.com/librtlsdr/librtlsdr/releases> (latest
  `win32-x64` zip) — or the RTL-SDR Blog driver release
  (<https://www.rtl-sdr.com/rtl-sdr-blog-v-3-downloads/>) which bundles the DLLs.
- Copy both DLLs next to `app.py`.

**4. Install and run:**

```bat
setup.bat          :: installs Python packages, checks the DLLs
run.bat            :: starts the server and opens your browser
```

Manual equivalent:

```bat
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8515**.

## Setup (Linux)

```bash
sudo apt install librtlsdr-dev rtl-sdr            # Debian/Ubuntu
sudo modprobe -r dvb_usb_rtl28xxu                 # free the stick for SDR mode
pip install -r requirements.txt
python3 app.py
```

macOS: `brew install librtlsdr`, then `pip install -r requirements.txt`
(radio works; TV does not).

---

## Using it

- **Spectrum**: the curve is the live band scan (2.4 MHz window). Peaks are
  stations. Click or drag anywhere to tune; the amber marker shows the tuned
  frequency; the waterfall below shows history.
- **Tuning**: arrows step by 100 kHz (FM) / 10 kHz (AM); type a frequency and
  press Enter. FM shows MHz, AM shows kHz.
- **Gain**: leave on Auto first. If FM stations distort, pick a fixed gain
  from the dropdown (e.g. 20–30 dB). In noisy environments lower gain often
  *improves* reception by avoiding ADC overload.
- **Audio buffer**: ~2.5 s of client-side jitter buffering for glitch-free
  listening (change `audio_target_buffer_s` in `config.json`).

## Antenna tips (stock telescopic/dipole)

- Extend it fully and keep it vertical for FM (broadcast FM is vertically or
  slant polarized; experiment).
- Stay **away from USB 3.0 ports, USB hubs and monitors** — USB 3 noise raises
  the noise floor across VHF. Use a rear USB 2.0 port.
- Put the dongle near a window for best results.
- AM (0.5–1.7 MHz) needs a long wire antenna; the stock whip is nearly deaf on
  AM indoors. Even 3–5 m of wire into the antenna input makes a big difference.

## TV (DVB-T) — read this

The RTL2832U chip contains a **hardware DVB-T demodulator**. Receiving TV is a
different mode from SDR mode, and its practicality depends on your platform
and region:

| Situation | TV support |
|---|---|
| Linux + DVB-T country (Europe, Asia-Pacific, Africa, parts of Middle East) | ✅ Full: channel scan + in-browser playback (this app's TV tab) |
| Windows (any region) | ❌ Needs Realtek's BDA driver which replaces the WinUSB SDR driver — the two modes can't coexist; the app shows a guide instead |
| North America (US/CA/MX) | ❌ Over-the-air TV is **ATSC** (8-VSB) — the RTL2832U hardware cannot decode it at all |

On Linux the TV tab becomes interactive: it switches driver expectations,
offers a **Scan for channels** button (writes `~/.config/dvb/channels.conf`),
and streams the selected channel through ffmpeg to the built-in video player.
Tools needed: `sudo apt install dvb-tools ffmpeg`. Switch back to radio with
`sudo modprobe -r dvb_usb_rtl28xxu`.

North American alternative: the same hardware is superb at **NOAA weather
radio** (162.400–162.550 MHz), airband, and ADS-B aircraft tracking — easy
follow-up projects with this app's DSP core.

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `port` | 8515 | Web server port |
| `de_emphasis_us` | 75 | FM de-emphasis (75 = Americas/Korea, 50 = most of Europe/Asia) |
| `default_band` / `default_freq_hz` | wfm / 98.3 MHz | Startup tuning |
| `audio_target_buffer_s` | 2.5 | Client jitter-buffer target |
| `sim_fallback` | true | Run a synthetic demo when no dongle is present |
| `lo_offset_hz` | 250000 | Offset tuning that dodges the RTL-SDR's center DC spike |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not open the RTL2832U` | Zadig WinUSB driver not bound yet (step 2); or SDR#/SDR++ is open and holds the stick — close it |
| `rtlsdr.dll not found` / pyrtlsdr import error | Copy `rtlsdr.dll` + `libusb-1.0.dll` next to `app.py` (step 3) |
| Device shows but tuning fails | Another process owns the stick; on Linux unload `dvb_usb_rtl28xxu` |
| Strong distortion on FM | Lower the gain (dropdown) — signal overload |
| No audio at all | Check the browser tab's autoplay block — click anywhere once, then reload |
| Everything noisy / ghost stations | Keep away from USB 3 ports and use a rear USB 2.0 port |
| Heard a single tone on every frequency | You are in simulation mode (no dongle detected) — the app says so in the header |

## Files

```
app.py               entry point (FastAPI + uvicorn)
backend/config.py    defaults + band tables
backend/device.py    RTL-SDR driver layer + synthetic simulator
backend/dsp.py       FM/AM demodulation, decimation, PSD
backend/stream.py    capture thread, tuning, websocket streaming
backend/tv.py        DVB-T detection, channel scan, TS streaming (Linux)
web/                 browser UI (spectrum, waterfall, players)
config.json          user settings
setup.bat / run.bat  Windows helpers
run.sh               Linux/macOS launcher
```
