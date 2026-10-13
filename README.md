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
             ├── Agent (v1.5)            scheduled background re-sweeps, natural-language
             │                           control, LLM station naming, dial report
             ├── Sound Studio (v1.6)     5-band EQ + compressor, presets auto-switched
             │                           by station type, output VU meter
             ├── RDS audit (v1.7)        dwells on every FM carrier and scores its
             │                           RDS reception (PI/PS/PTY/BER) 0-100
             ├── Digest (v1.7)           daily summary of scheduled-sweep preset
             │                           changes, plain-text log + optional email
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

**3. Native DLLs — automatic since v1.3.2** — `setup.bat` installs the
  `pyrtlsdrlib` package, which **ships the correct 64-bit librtlsdr DLL**
  (libusb statically linked) and pyrtlsdr picks it up by itself. No manual
  DLL download, no bitness traps. The app folder also contains a verified
  `dlls\` fallback (see `dlls\SOURCE.txt`).

  Only if you prefer manual DLLs: download the librtlsdr x64 release
  (<https://github.com/librtlsdr/librtlsdr/releases>) and copy
  `librtlsdr.dll` + `libusb-1.0.dll` next to `app.py` — they **must** match
  your Python's bitness (64-bit Python ⇔ x64 DLLs). "Wrong bitness" errors
  almost always come from 32-bit files in a package that looked 64-bit —
  `python check_dlls.py` reads the PE header of each DLL and tells you the
  *actual* architecture, not what the zip name claims.

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

### Dongle vs simulation

The app tries your dongle at startup; if it can't be opened it falls back to
the **simulated spectrum** (the amber dot in the header). Since v1.3.1 the
header pill has a switch button — click **Use dongle** to retry the hardware
immediately (no restart needed), or **Simulation** to go back to the demo
spectrum. If the dongle can't be opened, the status bar shows the exact
reason (missing DLLs, wrong bitness, WinUSB driver not bound via Zadig, or
another SDR program holding the stick).

## Setup (Linux)

```bash
bash run.sh
```

That is the whole install: the first run creates a virtualenv (`.venv`)
and installs the Python dependencies (no pip into the system Python —
Ubuntu 24.04 refuses that anyway), every later run just starts the
server. The server listens on the LAN and the startup banner prints the
exact URLs — from your desktop browser that is
`http://<server-ip>:8515`. If other machines still can't connect, open
the port in the firewall once: `sudo ufw allow 8515/tcp`.

For the dongle itself (DVB-TV driver blacklist, udev rules, libusb —
also the host-side prep for Docker):

```bash
sudo bash linux-setup.sh
```

Manual route, if you prefer to drive everything yourself:
`sudo apt install librtlsdr0 rtl-sdr`, `sudo modprobe -r
dvb_usb_rtl28xxu`, `pip install -r requirements.txt` (in a venv),
`python3 app.py` — add `--share` to make it reachable from other
machines.

macOS: `brew install librtlsdr`, then `bash run.sh`
(radio works; TV does not).

### "Server not reachable" from another machine (Linux)

Since v1.7.3 the page's offline banner matches your OS and points at
these fixes; the checks behind them:

| Check (on the server) | Command | Fix |
|---|---|---|
| app running at all? | `curl -s localhost:8515/api/status` | start it: `bash run.sh` |
| listening on the LAN, not just localhost? | `ss -tlnp \| grep 8515` → want `0.0.0.0:8515`, not `127.0.0.1:8515` | `bash run.sh` (passes `--share`) or `python3 app.py --share` |
| firewall in the way? | `sudo ufw status` | `sudo ufw allow 8515/tcp` |

### "The player does not see my dongle" (Linux)

Since v1.7.2 the web UI shows a **dongle diagnostics banner** the moment
the app falls back to the simulator — the banner states exactly which of
the four usual causes applies and the command that fixes it. The classic
line-up, in the order the probe checks them:

| Banner verdict | Meaning | Fix |
|---|---|---|
| *No RTL2832U found on the USB bus* | cable / port / power issue | replug, try another port, check `lsusb` for `0bda:2838` |
| *the DVB TV kernel driver has claimed it* | `dvb_usb_rtl28xxu` grabbed the stick (the #1 cause on Ubuntu) | `sudo modprobe -r dvb_usb_rtl28xxu` now + `sudo bash linux-setup.sh` to make it permanent (works for Docker too — the blacklist lives on the **host**) |
| *device node is not visible* | Docker without USB passthrough, or a missing `/dev/bus/usb` node | `--device=/dev/bus/usb` on `docker run` (docker-compose.yml already has it), replug on a bare host |
| *no read/write access* | udev permissions | `sudo bash linux-setup.sh` (installs rules for `0bda:2832/2838`), then replug |
| *USB side looks healthy* | something else | another SDR program may hold the stick, or librtlsdr failed to load — see the error line under the header |

The same probe backs the API (`GET /api/status` → `usb_probe`), so you can
check from the shell: `curl -s localhost:8515/api/status | python3 -m json.tool | grep -A6 usb_probe`.

### "Fatal Python error: Segmentation fault" at startup (Linux)

Fixed in v1.7.5. The console showed the dongle being found
(`Found Rafael Micro R820T/2 tuner`, `Disabled direct sampling mode`) and
the process dying with a segfault right after. Cause: pyrtlsdr's loader
always tries the **librtlsdr snapshot bundled inside the pyrtlsdrlib
wheel first** — an old build that ships its own ancient libusb/libudev,
and it crashes inside `rtlsdr_read_sync` on modern kernels (Ubuntu
24.04). The distro build, compiled against the system libusb, is what
every Linux SDR tool uses.

Since v1.7.5 the player prefers the distro library automatically; just
make sure it is installed (once):

```bash
sudo apt install librtlsdr0 rtl-sdr     # or: sudo bash linux-setup.sh
bash run.sh
```

On Ubuntu the package lives in the **universe** component; minimal server
images sometimes ship without it. If apt says `Unable to locate package
librtlsdr0`: `sudo add-apt-repository universe && sudo apt update` first
(or let `linux-setup.sh` do it — since v1.7.6 it enables universe and, as
a last resort, installs the .deb directly).

The console prints the exact binary in use, and
`curl -s localhost:8515/api/status | python3 -m json.tool | grep librtlsdr`
shows it too — it should be a system path like
`/usr/lib/x86_64-linux-gnu/librtlsdr.so.0`, never a
`.../site-packages/pyrtlsdrlib/...` path. Since v1.7.7 a missing system
library is loud, not silent: if the player would fall back to the
crash-prone bundled build, the console prints a WARNING with the fix, and
every dongle open announces the native library path it is about to use.
`rtl_test` (from the `rtl-sdr`
package) is a handy independent check that the distro library and dongle
cooperate. Escape hatch if you ever need the wheel's build back:
`RTLSDR_PLAYER_FORCE_BUNDLED=1 bash run.sh`.

---

## Setup (Ubuntu server / Docker)

The repo ships a Dockerfile (Ubuntu 24.04 base) so the receiver can run
headless on a server with the dongle plugged in:

```bash
# one-time host prep
curl -fsSL https://get.docker.com | sudo sh        # or: sudo apt install docker.io docker-compose-v2
sudo bash linux-setup.sh                           # DVB blacklist + udev rules (host side)

# build + run
git clone https://github.com/jwfrancois/rtl-sdr-player.git
cd rtl-sdr-player
docker compose up -d --build
```

Then open `http://<server-ip>:8515` from any browser on your LAN.

What compose sets up for you:

* **USB passthrough** — `devices: ["/dev/bus/usb:/dev/bus/usb"]` makes the
  dongle visible inside the container (no udev rules needed: the container
  runs as root).
* **One persistent volume** — everything that changes at runtime lives in
  `/app/data` (the image sets `RTLSDR_DATA_DIR`): the 20 presets,
  bookmarks, agent/LLM settings, the PI cache, digest texts and audit
  reports. `docker compose down/up` keeps it; delete the `rtl-sdr-data`
  volume to reset.
* **Healthcheck** — the container self-reports based on `GET /api/status`.
* **Sim fallback** — with no dongle passed through, the app serves the
  simulated spectrum so you can still reach the UI.

Prefer plain `docker run`?

```bash
docker build -t rtl-sdr-player .
docker run -d --name rtl-sdr-player -p 8515:8515 --restart unless-stopped \
  --device=/dev/bus/usb -v rtl-sdr-data:/app/data rtl-sdr-player
```

Notes: the digest's daily build hour follows the container's local time
(`TZ` in docker-compose.yml); SMTP for the email digest is configured in
the UI (AGENT panel → DIGEST). Manual Linux install (no Docker) still
needs `librtlsdr-dev` per the section above — or just `libusb-1.0-0` when
pyrtlsdrlib's bundled librtlsdr is used.

---

## What's new in v1.7.0

**Scheduled-sweep change digest.** Every sweep, preset-bank change, PI
lookup rename, AI naming round and RDS audit lands in a small journal.
Once a day — at the hour you pick in the AGENT panel's DIGEST row — the
receiver compiles the day's activity into a plain-text summary (sweeps
run, presets added/removed/renamed with before→after names, AM noise
floor, audit scores) and writes it to `<data>/state/digests/YYYY-MM-DD.txt`.
Tick **email** and fill the SMTP settings to have the same text mailed to
you (STARTTLS on port 587, SSL on 465, any standard provider or a local
MTA; the password stays in `agent_config.json` on your machine). If the
app was down at the scheduled hour, the missed day is built on the next
start (catch-up). **Build now** compiles today's digest immediately;
**Send now** builds + emails; the agent also answers `digest` and
`send digest`.

**Spectrum-wide RDS auditing.** The sweep tells you *what* is on air;
the audit tells you *how well each station decodes*. Press **RDS audit**
(SCANNER panel) and the receiver dwells 3–12 s (select) on every FM
carrier from the last sweep plus the FM presets — up to 80 channels —
recording: PI stability, PS completeness (partial names count), programme
type, RadioText, stereo pilot, peak channel power, RDS group rate and an
approximate block-error ratio. Those combine into a 0–100 quality score
per station; the report shows the twelve weakest rows with a color-coded
score badge and stays available across restarts (`state/audits/`, last
10 kept, `GET /api/audit/report`). Tuning is restored afterwards, audio
mutes during the pass, a manual tune stops it cleanly, and scheduled
sweeps + audits cooperate through the same scan state machine.

**Docker on an Ubuntu server.** A Dockerfile + docker-compose.yml turn
the receiver into a headless LAN service (see *Setup (Ubuntu server /
Docker)* above): Ubuntu 24.04 base, bundled librtlsdr via pyrtlsdrlib,
USB passthrough, one persistent volume for all runtime state via the new
`data_dir` / `RTLSDR_DATA_DIR` setting, and a healthcheck. The same
`data_dir` config works outside Docker when you want state out of the
app directory.

---

## What's new in v1.6.0

**Preset change notifications.** Every change to the 20-preset bank
(auto-fill after a scheduled re-sweep, fill-from-sweep, online PI lookup
renames, AI naming) bumps a change counter and leaves a diff (added /
renamed / removed). Enable **Notify** in the STATIONS header and the
browser raises a desktop notification whenever the bank changes — with
in-page toast fallback when notifications are blocked. Your own edits
from the same tab do not ping you.

**PI-based online callsign lookup.** RDS gives the PI code even when the
station name never decodes. After each sweep the receiver asks online PI
databases (fmscan.org, fmdx.pl, or your own URL template — Agent →
`pi_lookup_url`) to resolve PI codes that only got a generic "FM 96.1"
name, then renames the sweep results and the matching unlocked presets.
Results are cached in `pi_cache.json` (positive 30 days, negative 6 h),
everything degrades gracefully offline, and `GET /api/pi/2A5B` plus the
agent command *"lookup pi 2A5B"* do the same on demand.

**AM sweep sensitivity.** A fixed absolute threshold cannot work on AM:
the noise floor moves with daytime/nighttime propagation and local QRM.
Before the AM segment of a sweep, a probe pass measures the band's noise
floor and the AM threshold becomes floor + margin — **high** = 3 dB,
**medium** = 6 dB, **low** = 12 dB (select next to the SWEEP row,
persisted in the agent config). The measured floor and threshold are
shown under the sweep row, and AM channels get a longer dwell because
direct-sampling retunes settle slowly.

**Sound Studio.** A proper audio chain between the demodulator and your
speakers: 5-band EQ (bass / low mid / mid / presence / treble) plus a
dynamics compressor. Seven presets — Studio, Music, Speech, AM Warm,
NOAA, Airband, Night — and an **AUTO** mode that picks the right one from
the station type: NOAA weather radio gets alert clarity, airband gets a
comms bandpass with firm compression, speech PTYs (News, Talk, Sports…)
get an intelligibility curve, music gets a smiley, AM gets its warmth.
Per-preset EQ trims are saved in the browser, and an output VU meter with
peak indicator closes the loop.

**Under the hood.** The spectrum websocket now sends a prebuilt snapshot
(prepared on the capture thread) instead of touching engine internals
from the event loop, sends lightweight progress-only frames during
sweeps, and every websocket send runs under a watchdog so a wedged
transport can never freeze a handler — clients reconnect transparently.
The simulated AM generator was also fixed to keep up with realtime (it
was ~2.6x too slow, which made sim-AM audio stretch and the pipeline
starve), and sweep AM scans are stable on long runs.

## What's new in v1.5.0

**The agent: your receiver runs itself.**

- **Scheduled background re-sweeps** — pick *15 min / 30 min / 1 h / 4 h* in the
  AGENT panel (or just tell the app "sweep every 30 minutes") and the receiver
  re-sweeps all bands by itself, **auto-refills the 20 presets** and leaves a
  note ("sweep done: auto-filled 18 presets"). Slots you named by hand
  (double-click rename, shown with an amber right edge) are **kept** — the
  scheduler refills around them and never double-books their channel. By
  default a scheduled sweep waits until no browser tab is listening; tick
  **while listening** to let it interrupt (audio mutes during the sweep, your
  station comes back after).
- **Natural-language control** — the AGENT panel has a command box:
  `tune 101.7` · `am 1010` · `121.5` · `162.550` · `scan airband` ·
  `scan noaa` · `sweep all` · `sweep fm wx` · `rescan` · `stop` ·
  `next station` · `previous station` · `report` · `what am i listening to` ·
  `sweep every 30 minutes` · `sweeps off` · `help`. Rule-based, instant, and
  it works without any LLM.
- **Dial report** — **Report** answers "what's on air?": what you're tuned to,
  what the last sweep found (strongest stations with names), how many presets
  are filled, and what to do next.
- **LLM station naming (optional)** — configure any OpenAI-compatible endpoint
  (OpenAI, Z.ai GLM, local Ollama / LM Studio, vLLM) under *AGENT → LLM
  settings* and press **AI names**: the model proposes display names + genre
  tags for your presets (marked ✦ on the slot; your original name stays
  untouched underneath, and the tooltip shows both). The key stays in
  `agent_config.json` on this machine. When enabled, the agent also
  understands fuzzier commands and adds a narrative to the dial report, and
  scheduled sweeps can auto-name new stations (`auto_enrich`).
- PI codes from RDS are stored with presets and shown to the LLM as hints
  (unambiguous US callsign candidates are decoded from the NRSC-4 table).

## What's new in v1.4.0

- **All-band sweep** — one "All bands" button sweeps every channel of the
  bands you tick (FM 87.5–108, AM 530–1700 kHz, NOAA WX, airband
  118–137 MHz — 1,091 channels in ~6–7 min, less for a single band),
  measures RF channel power at each one and remembers every station above
  the squelch threshold. Audio is muted while sweeping; your tuning is
  restored afterwards. Optional frequency window (`from_hz`/`to_hz` in the
  API) sweeps just a segment.
- **20 named station presets** — a new STATIONS panel gives you 20 slots:
  click tunes, right-click saves the current station, double-click
  renames, "Fill from sweep" populates all 20 from the last sweep
  (strongest first, adjacent-channel shoulders merged). Slots persist in
  `stations.json`.
- **Automatic station names** — FM stations are named from their RDS PS
  broadcast (the sweep dwells up to ~3 s extra on hot FM channels to
  decode it), NOAA channels get their standard WX1–WX7 names, known
  airband channels keep their labels (GUARD, CTAF, UNICOM…), everything
  else is named by frequency. You can always rename.
- **Full-band scan lists** — the live (listen-and-hold) scanner gained
  "Full FM broadcast sweep" and "Full AM sweep" lists alongside the
  existing NOAA/airband ones.
- **Better WFM signal metric** — FM channel power is now measured
  pre-detection on the RF signal (like the NBFM/AM squelch metric) instead
  of demodulated audio loudness, which depended on how loud the programme
  happened to be. This is what the sweep scores and what the LVL meter
  shows on the FM band.

## What's new in v1.3.5

- **Choppy audio on real hardware fixed** — the old capture loop did the
  USB read *and* all Python DSP serially, so every block took
  `block_duration + dsp_time` to consume: a permanent ~15–30 % deficit
  that slowly drained the player's 2.5 s jitter buffer and kept it
  underrunning. USB ingestion now runs on its own thread that feeds a
  bounded block queue; librtlsdr's transfer ring is never left starving,
  and DSP time no longer subtracts from the realtime budget.
- **Short USB reads are no longer thrown away** — the old loop discarded
  up to half a block (≈ 55 ms of audio) whenever a read came back short;
  partial reads now accumulate into the next full block.
- **Retunes park the USB thread** through a clean handshake, so the
  sample rate never changes mid-read.
- **Audio-gap diagnostics** — the player counts underruns ("gaps N" in
  the status bar) and `/api/status` now reports `capture` stats
  (`usb_overflows`, `short_reads`, `dsp_ms`, `queue_blocks`), so a
  remaining problem can be diagnosed from numbers instead of vibes.

## What's new in v1.3.4

- **First-hardware-launch crash fixed** — with the dongle finally opening
  (v1.3.2's DLL work), startup died at `set_direct_sampling("disabled")`
  with `SyntaxError: invalid value "disabled"` on current pyrtlsdr builds.
  pyrtlsdr changed its accepted spellings across releases (some take
  `'i'`/`'q'`, some `'I'`/`'Q'`, none take `'disabled'`), so the app now
  sends the integers the C API actually uses (0 = off, 1 = I, 2 = Q) and
  falls back to string spellings only if a build refuses ints. Verified
  against the real installed pyrtlsdr: the exact reported SyntaxError
  reproduces without the fix and disappears with it.

## What's new in v1.3.3

- **The page now tells you when the server is down** — if `run.bat` was
  closed (or the PC rebooted), the browser page used to sit silent while
  the console filled with `ERR_CONNECTION_REFUSED`. It now shows an amber
  **"Server not reachable — run run.bat"** banner, keeps polling, and
  reconnects audio + spectrum by itself the moment the server returns.
- **Volume slider actually works** — the audio worklet read a property
  that does not exist on `AudioWorkletProcessor` (`this.parameters`), so
  every page load threw a `TypeError` and slider moves were ignored.
  Volume is now stored and applied in the processor's render callback.
- **Calmer reconnects** — websocket retry intervals grow while the server
  is down (2.5 → 10 s) instead of hammering every 2.5 s forever.
- **`run.bat` stays open on errors** — a failed start (Python missing,
  port busy, crash) no longer flashes a window and vanish; the reason
  stays on screen until you press a key.

## What's new in v1.3.2

- **No more DLL hunting** — `pyrtlsdrlib` (added to `requirements.txt`)
  ships a prebuilt **64-bit librtlsdr with libusb statically linked**;
  pyrtlsdr loads it automatically, so `setup.bat` alone gets the dongle
  ready. Manual `rtlsdr.dll` downloads are no longer needed.
- **Bundled fallback DLLs** — a verified x64 `librtlsdr.dll` +
  `libusb-1.0.dll` now ship in the app's `dlls\` subfolder (origin and
  licenses in `dlls\SOURCE.txt`), searched automatically at startup.
- **Fact-based bitness diagnostics** — the app reads the PE header of every
  DLL it finds, so the status bar reports the *actual* architecture
  ("rtlsdr.dll is 32-bit (x86) but your Python is 64-bit") instead of
  guessing from the download you thought you made. `check_dlls.py` reports
  the same, plus `pyrtlsdrlib` status, and names the exact file to delete.

## What's new in v1.3.1

- **The DLLs-in-the-app-folder fix** — on Python 3.8+, native DLLs dropped
  next to `app.py` were silently invisible to `pyrtlsdr` (Windows stopped
  searching the application folder years ago). The app now registers the
  app folder (and its `dlls\` subfolder) with `os.add_dll_directory()` at
  startup, so `rtlsdr.dll` + `libusb-1.0.dll` are finally found exactly
  where step 3 says to put them.
- **Precise DLL diagnostics** — when the dongle still can't open, the status
  bar no longer guesses: it distinguishes *DLLs not found*, *wrong bitness*
  (WinError 193 → "your DLLs are 32-bit but Python is 64-bit"), *missing
  dependency* (WinError 126 → libusb), and *driver not bound* (Zadig).
  `GET /api/device/dlls` returns the full report and **`check_dlls.py`**
  (also run by `setup.bat`) prints it in the console.
- **Dongle ↔ simulation switch button** — the header device pill gains a
  small button: **Use dongle** retries the real RTL-SDR instantly (engine
  hot-swap, no restart; audio clients re-subscribe automatically) and
  **Simulation** returns to the synthetic demo spectrum. A failed switch
  never takes the app down — it shows the reason instead.

## What's new in v1.3

- **CTCSS tone squelch (WX band)** — narrowband FM channels gain the classic
  PL gate: pick one of the 38 standard EIA tones (67.0–250.3 Hz) in the CTC
  dropdown and the audio only opens when the carrier *and* the matching
  sub-audible tone are present. The decoder is a Goertzel filter tapped
  before the voice high-pass, checks the tone's deviation (~150 Hz minimum),
  its frequency stability against neighbouring bins (so wobbly voice
  fundamentals don't false-trigger), and needs ~0.5 s of solid tone before
  opening. The scanner respects it too: with a tone set, it only holds on
  channels that carry *your* tone. The squelch badge shows `CTC 141.3 ✓`
  when the tone locks.
- **Waterfall zoom** — scroll the mouse wheel over the spectrum or waterfall
  to zoom in around the cursor (up to ~50×, down to a ~50 kHz sliver),
  double-click or the ⤢ button to reset, +/− buttons step in and out. The
  zoom level shows in the spectrum header next to the visible span, the
  frequency axis relabels itself, the waterfall crops to the same window,
  and click-to-tune maps correctly at any zoom level. The zoom window
  follows retunes within the band and resets on band change.
- **SDR#-style bookmarks** — tune anywhere, press **+ Add** (name is
  pre-filled with the frequency, edit it before Enter), and the station is
  saved with its band and demod mode to `bookmarks.json`. Click a bookmark
  to jump — the band, frequency *and* mode (e.g. USB) are all restored.
  ✕ deletes. Bookmarks survive restarts and are shared across browser tabs.

## What was new in v1.2

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
  frequency; the waterfall below shows history. Wheel = zoom, double-click =
  reset (see v1.3 above).
- **Tuning**: arrows step by band (100 kHz FM / 10 kHz AM / 25 kHz WX+AIR);
  type a frequency and press Enter. FM shows MHz, AM shows kHz.
- **Presets**: WX and AIR tabs show clickable channel chips (WX1–WX7, Guard,
  CTAF…). The active one is highlighted.
- **Agent (AGENT panel)**: a command box that controls the receiver in plain
  English (see v1.5 above), a **Report** button (what's on air), and an
  **AI names** button. RE-SWEEP schedules the background all-band sweep:
  *off / 15 min / 30 min / 1 h / 4 h*, with **fill** (auto-refill the 20
  presets afterwards) and **while listening** (allow sweeps while a tab is
  open) toggles. The status line under it counts down to the next sweep and
  reports the last one's outcome. *LLM settings* stores an OpenAI-compatible
  base URL + model + API key for AI naming and fuzzy commands.
- **Stations (20 presets)**: your own bank of named stations. Click a slot
  to tune, **right-click** to save the current station into it (named
  automatically from RDS / the WX table), **double-click** to rename —
  renamed slots are locked (amber right edge) and survive re-fills.
  See the sweep below for the one-click way to fill all 20.
- **Sweep (SCANNER panel, every band)**: tick the bands you want
  (FM / AM / WX / AIR) and press **All bands**. The receiver visits every
  channel, measures signal power, and decodes RDS names on hot FM
  channels — progress shows in the panel and results appear as a list
  (strongest first, click a row to tune). When it finishes, press
  **Fill from sweep** in the STATIONS panel to write the top 20 into your
  presets. Your tuning is restored after the sweep; the SQL slider doubles
  as the sweep threshold.
- **Live scanner**: pick a list (NOAA, key airband, full airband,
  full FM, full AM) and press Scan — it hops and *holds* on active
  channels (v1.1 behaviour), unlike the sweep which just measures.
- **Mode chips** (AIR/AM bands): AM / USB / LSB. In USB or LSB the BFO slider
  appears — drag to de-tune until voices sound natural, press **0** to reset.
- **Bookmarks**: + Add saves the current band/frequency/mode under a name;
  click a row to jump back, ✕ removes it.
- **CTCSS** (WX band): the CTC dropdown under the LVL meter selects the tone
  (or *CTCSS off* for plain carrier squelch).
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
everything: six FM stations (the ones at **96.1 MHz `SIM-RDS`** and
**101.7 MHz `JAZZ-FM`** carry full RDS + stereo-pilot test signals), four
AM stations, airband AM on 121.5/122.8/123.1, **SSB voice on 119.200 (USB)
and 123.450 (LSB)**, and NOAA voice on 162.400 (**CTCSS 123.0**) / 162.550
(**CTCSS 141.3**). Start it explicitly with `python app.py --sim`.
Try the v1.4 flow in sim: SWEEP → All bands (a few minutes) →
Fill from sweep → your 20 presets fill with named stations.

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
| `sweep_dwell_s` | 0.35 | Seconds spent per channel in the all-band sweep |
| `sweep_rds_wait_s` | 3.0 | Extra dwell on hot FM channels while decoding the RDS name |
| `scan_memory_file` | scan_memory.json | Persisted scanner hit memory (smart sorting) |
| `bookmarks_file` | bookmarks.json | Persisted SDR#-style bookmark list |
| `stations_file` | stations.json | Persisted 20-slot named station presets |
| `sweep_results_file` | sweep_results.json | Persisted last sweep results (feeds Fill from sweep) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not open the RTL2832U` | Zadig WinUSB driver not bound yet (step 2); or SDR#/SDR++ is open and holds the stick — close it |
| Page shows "Server not reachable" | The player app is not running (or was closed). Double-click `run.bat` and leave its window open — the page reconnects by itself |
| `rtlsdr.dll not found` / DLL warning at the bottom | Run `setup.bat` (installs `pyrtlsdrlib` = automatic DLL); or copy the DLLs from the app's `dlls\` subfolder next to `app.py`; `python check_dlls.py` names the exact problem |
| WinError 193 in the status bar | The DLL file itself is the wrong bitness (the zip name lied). Delete that DLL from the app folder and run `setup.bat` — or replace it with the x64 copy from the app's `dlls\` subfolder |
| `SyntaxError: invalid value "disabled"` at startup (fixed in 1.3.4) | Old app + new pyrtlsdr clash over direct-sampling spellings — update to v1.3.4+ |
| Dongle won't switch on / falls back to simulation | Click **Use dongle** in the header; the status bar then shows the current reason (DLLs, Zadig driver, or stick busy) |
| Device shows but tuning fails | Another process owns the stick; on Linux unload `dvb_usb_rtl28xxu` |
| Strong distortion on FM | Lower the gain (dropdown) — signal overload |
| No RDS on a known-RDS station | Check the STEREO flag: without the 19 kHz pilot there is no RDS; try a fixed gain or reposition the antenna |
| RDS takes a few seconds | Normal — PS needs all four 2-char segments; the decoder first locks carrier + bit timing |
| Scanner never opens squelch | Raise the LVL bar reading above your SQL value by lowering SQL; check the antenna |
| Scanner parks on noise | Raise SQL (more positive dB) |
| No audio at all | Check the browser tab's autoplay block — click anywhere once, then reload |
| Choppy / gappy audio | Fixed in v1.3.5 (USB reads moved to a dedicated thread) — update. Still gappy? Watch "gaps N" in the status bar, and `/api/status` → `capture`: rising `usb_overflows` = PC too slow (close heavy apps), rising `short_reads` = USB link trouble (use a rear USB 2.0 port, no hub) |
| Everything noisy / ghost stations | Keep away from USB 3 ports and use a rear USB 2.0 port |
| Heard a single tone on every frequency | You are in simulation mode (no dongle detected) — the app says so in the header |

## Files

```
app.py               entry point (FastAPI + uvicorn)
check_dlls.py        Windows DLL diagnostic (bitness, PE arch, load test)
dlls/                verified x64 librtlsdr + libusb fallback DLLs (+SOURCE.txt)
backend/config.py    defaults + band/preset/mode/scan tables
backend/device.py    RTL-SDR driver layer + synthetic simulator (RDS + SSB test signals)
backend/dsp.py       FM/AM/NBFM/SSB demodulation, CTCSS decoder, pilot tracker, decimation, PSD
backend/rds.py       RDS decoder (Costas+Gardner+CRC) + test-signal encoder
backend/agent.py     agent layer: NL commands, dial report, LLM naming client
backend/stream.py    capture thread, tuning, squelch + CTCSS gate, scanner + memory, bookmarks, streaming, sweep scheduler
backend/tv.py        DVB-T detection, channel scan, TS streaming (Linux)
web/                 browser UI (spectrum + waterfall zoom, RDS, scanner, CTCSS, bookmarks)
config.json          user settings
agent_config.json    agent settings (created on first change; sweep schedule + LLM key, local only)
setup.bat / run.bat  Windows helpers
run.sh               Linux/macOS launcher
tests/               self-test suite (codec, DSP chain, engine, REST/WS smoke)
.github/workflows/   CI: runs the test suite on push (ubuntu + windows)
```
