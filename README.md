# RTL-SDR Player

A local web receiver for the **RTL-SDR Blog v3** (RTL2832U + R820T2): play
over-the-air **radio** and **DVB-T TV** from your dongle, with a live spectrum
analyzer and waterfall, right in your browser. Built and pinned for the **USA**.

```
Dongle → [this app] → http://localhost:8515
             │
             ├── FM band 87.5–108 MHz    wideband FM + RDS/RBDS (US tables)
             ├── AM band 530–1700 kHz    direct-sampling (Q) mode (v3 feature),
             │                           plus USB/LSB for shortwave SSB
             ├── WX tab                  NOAA Weather Radio 162.400–162.550
             │                           (narrowband FM, 7 channel presets)
             ├── AIR tab                 US airband 118–137 MHz: AM + USB/LSB,
             │                           squelch scanner + station memory
             ├── Live spectrum + waterfall with click-to-tune
             └── TV tab                  DVB-T capability detection + guide
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

## What's new in v1.2

- **SSB (USB/LSB) demodulation** — the AIR and AM bands gain single-sideband
  via a phasing demodulator (one-sided 300–3175 Hz voice bandpass on the
  complex baseband). Listen to military/utility SSB users around the airband
  and shortwave-style SSB on the AM band. A **BFO clarifier** slider
  (±3 kHz, phase-continuous) fine-tunes the pitch, with a zero button.
- **NBFM/AM squelch everywhere** — squelch now engages **automatically** when
  you switch to the WX or AIR band (that's what those bands are for) and
  releases again on FM/AM broadcast, until you touch the SQL slider yourself.
  Closing now uses **hysteresis** (4 dB below the open threshold) plus the
  0.5 s tail, so channels don't stutter on/off at the threshold. A live
  SQUELCH OPEN/CLOSED badge shows the gate state.
- **Scan memory with smart sorting** — every scanner hold is memorized
  (frequency, hit count, last-heard time) in `scan_memory.json` and survives
  restarts. Pick **Smart: most active first** and the scanner visits the
  busiest channels first; the MEMORY table (top hits, clickable) and a Clear
  button live in the scanner panel.

## What was new in v1.1 (USA edition)

- **75 µs de-emphasis pinned** — the FM de-emphasis time constant and the
  RBDS program-type table are set for the US (`"region": "US"` in
  `config.json`; European listeners would set `50` — but you're in the US,
  so it's already right).
- **RDS station names** — on the FM band the app decodes the 57 kHz RDS
  subcarrier: program service name (big amber readout), PI code, program
  type (RBDS names), RadioText, alternative frequencies and even the
  station's clock-time group. It measures your dongle's clock error from the
  19 kHz pilot tone (the ppm readout is real) and phase-locks the decoder
  with a Costas loop. Expect a lock a couple of seconds after tuning to a
  station with RDS.
- **NOAA Weather Radio presets** — the WX tab tunes 162.400–162.550 MHz with
  narrowband FM demodulation (5 kHz deviation, voice-band shaping) and all
  seven WX1–WX7 channels one click away.
- **Airband scanner** — the AIR tab receives 118–137 MHz AM and adds a
  scanner: pick *Key US airband channels* (Guard 121.5, CTAF/UNICOM, air-to-air,
  FSS…), *NOAA weather* or a *full 118–137 MHz sweep* (760 channels, ~6 min).
  It hops with a channel-power squelch you can tune live (the LVL bar shows
  the received channel power against your SQL threshold), holds while a
  transmission is active, releases 2.5 s after the channel goes quiet, and
  logs every hit with a timestamp.

### How the squelch works

The scanner (and the SQL slider on WX/AIR bands) gates audio on **channel
power**: the energy inside the 8 kHz-wide channel after filtering, in dB.
Noise-only channels sit around −50…−35 dB; any real carrier lands well above
−20 dB. Start at −20 dB and adjust: too high and weak stations never open the
gate, too low and the scanner parks on noise. The gate opens at the SQL
value and only closes 4 dB *below* it (hysteresis) after 0.5 s of quiet —
squelch doesn't chatter on marginal carriers.

## Using it

- **Spectrum**: the curve is the live band scan (2.4 MHz window). Peaks are
  stations. Click or drag anywhere to tune; the amber marker shows the tuned
  frequency; the waterfall below shows history.
- **Tuning**: arrows step by band (100 kHz FM / 10 kHz AM / 25 kHz WX+AIR);
  type a frequency and press Enter. FM shows MHz, AM shows kHz.
- **Presets**: WX and AIR tabs show clickable channel chips (WX1–WX7, Guard,
  CTAF…). The active one is highlighted.
- **Mode chips** (AIR/AM bands): AM / USB / LSB. In USB or LSB the BFO slider
  appears — drag to de-tune until voices sound natural, press **0** to reset.
- **RDS panel** (FM tab): appears under the demod info line. PS name lights
  up first (it completes after all four 2-character segments arrive),
  RadioText and AF list follow. STEREO = 19 kHz pilot detected.
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
- NOAA WX and airband work fine on the stock whip — they're strong VHF
  services.

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

North American reality check: with ATSC off the table, this dongle's best
OTA tricks are exactly what v1.1 ships — NOAA weather radio, airband
scanning, and RDS-decoding FM.

## Simulation mode

With no dongle present the app runs a synthetic RF spectrum so you can try
everything: six FM stations (the one at **96.1 MHz carries a full RDS +
stereo-pilot test signal** — PI `2A5B`, PS `SIM-RDS`), four AM stations,
airband AM on 121.5/122.8/123.1, **SSB voice on 119.200 (USB) and 123.450
(LSB)**, and NOAA voice on 162.400/162.550. Start it explicitly with
`python app.py --sim`.

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `port` | 8515 | Web server port |
| `region` | "US" | Pins RBDS PTY names (USA program-type table) |
| `de_emphasis_us` | 75 | FM de-emphasis (75 = Americas/Korea, 50 = EU/Asia) |
| `default_band` / `default_freq_hz` | wfm / 98.3 MHz | Startup tuning |
| `audio_target_buffer_s` | 2.5 | Client jitter-buffer target |
| `sim_fallback` | true | Run a synthetic demo when no dongle is present |
| `lo_offset_hz` | 250000 | Offset tuning that dodges the RTL-SDR's center DC spike |
| `squelch_default_db` | -20 | Channel-power squelch threshold used by the scanner |
| `squelch_hysteresis_db` | 4.0 | Close threshold sits this far below the open threshold |
| `squelch_auto_bands` | {"wx": true, "air": true} | Bands that get squelch automatically on switch |
| `scan_dwell_s` | 0.45 | Seconds spent per channel while hopping |
| `scan_release_s` | 2.5 | Squelch-closed time before leaving a held channel |
| `scan_memory_file` | scan_memory.json | Persisted scanner hit memory (smart sorting) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not open the RTL2832U` | Zadig WinUSB driver not bound yet (step 2); or SDR#/SDR++ is open and holds the stick — close it |
| `rtlsdr.dll not found` / pyrtlsdr import error | Copy `rtlsdr.dll` + `libusb-1.0.dll` next to `app.py` (step 3) |
| Device shows but tuning fails | Another process owns the stick; on Linux unload `dvb_usb_rtl28xxu` |
| Strong distortion on FM | Lower the gain (dropdown) — signal overload |
| No RDS on a known-RDS station | Check the STEREO flag: without the 19 kHz pilot there is no RDS; try a fixed gain or reposition the antenna |
| RDS takes a few seconds | Normal — PS needs all four 2-char segments; the decoder first locks carrier + bit timing |
| Scanner never opens squelch | Raise the LVL bar reading above your SQL value by lowering SQL; check the antenna |
| Scanner parks on noise | Raise SQL (more positive dB) |
| No audio at all | Check the browser tab's autoplay block — click anywhere once, then reload |
| Everything noisy / ghost stations | Keep away from USB 3 ports and use a rear USB 2.0 port |
| Heard a single tone on every frequency | You are in simulation mode (no dongle detected) — the app says so in the header |

## Files

```
app.py               entry point (FastAPI + uvicorn)
backend/config.py    defaults + band/preset/mode/scan tables
backend/device.py    RTL-SDR driver layer + synthetic simulator (RDS + SSB test signals)
backend/dsp.py       FM/AM/NBFM/SSB demodulation, pilot tracker, decimation, PSD
backend/rds.py       RDS decoder (Costas+Gardner+CRC) + test-signal encoder
backend/stream.py    capture thread, tuning, squelch, scanner + memory, websocket streaming
backend/tv.py        DVB-T detection, channel scan, TS streaming (Linux)
web/                 browser UI (spectrum, waterfall, RDS + scanner + memory panels)
config.json          user settings
setup.bat / run.bat  Windows helpers
run.sh               Linux/macOS launcher
tests/               self-test suite (codec, DSP chain, engine, REST/WS smoke)
.github/workflows/   CI: runs the test suite on push (ubuntu + windows)
```
