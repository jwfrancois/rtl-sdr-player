# Worklog

---
Task ID: 1
Agent: main (Super Z)
Task: RTL-SDR v3 player software — research + build

Work Log:
- Clarified requirements via AskUserQuestion: Windows desktop, browser UI, spectrum+waterfall,
  smooth ~2.5s audio buffer, pip+script install, stock antenna, TV standard "not sure" (auto-detect)
- Web research: Windows Zadig WinUSB + rtlsdr.dll/libusb DLL path confirmed; DVB-T on RTL2832U
  requires BDA driver on Windows (conflicts with WinUSB) / clean path on Linux via dvb_usb_rtl28xxu;
  WFM demod = decimate -> polar discriminator -> de-emphasis; offset tuning avoids DC spike;
  AM band via direct-sampling (Q) mode on v3
- Plan: FastAPI + pyrtlsdr local web app at http://localhost:8515, sim-mode fallback when no
  dongle present (sandbox demo), deliverable zip in /home/z/my-project/download/

Stage Summary:
- Requirements locked; research JSON in /home/z/my-project/research/
- Building app in /home/z/my-project/download/rtl-sdr-player/

---
Task ID: 2
Agent: main (Super Z)
Task: Build, test, and package the RTL-SDR Player

Work Log:
- Built full stack: backend/config.py, device.py (pyrtlsdr wrapper + SimDevice fallback),
  dsp.py (NCO offset-tuning, FIR decimation chains, WFM polar discriminator + de-emphasis,
  AM envelope + AGC, Welch PSD), stream.py (capture thread, per-subscriber audio queues,
  generation-based retune), tv.py (DVB-T detection, Linux dvbv5-scan/zap + ffmpeg pipeline,
  Windows/NA guide); app.py FastAPI with /api/* REST + /ws/audio + /ws/spectrum
- Frontend: dark instrument-style UI (index.html/style.css/app.js/worklet.js) — canvas
  spectrum + waterfall with click/drag-to-tune, AudioWorklet jitter-buffer player
  (2.5 s target), TV capability panel, live status bar
- Smoke tests (scripts/smoke_test.py): REST tune/clamp/gain OK; spectrum 512 bins with
  station peaks ~60 dB over floor; audio 2.6 s PCM per 2 s wall, RMS 4786 (audible)
- Headless browser verification (agent-browser): UI renders, tuning works, waterfall
  accumulates, TV tab shows capability report; fixed bugs found during testing:
  (a) boot order blocked sockets on suspended AudioContext -> sockets now independent,
  (b) [hidden] overridden by author CSS -> added [hidden]{display:none!important},
  (c) shared PCM deque let multiple WS consumers steal audio -> per-subscriber
      broadcast queues + client reconnect seq guards; verified two simultaneous
      clients each receive full stream; worklet buffer fill 5.1 s confirmed via
      port query hook
- Packaged rtl-sdr-player.zip (20 files) + screenshot into /home/z/my-project/download/

Stage Summary:
- Deliverable: /home/z/my-project/download/rtl-sdr-player.zip (also unzipped at
  download/rtl-sdr-player/) — Windows-first local web receiver, sim fallback for
  no-dongle machines, honest TV capability detection (DVB-T on Linux only)
- Verified end-to-end in sim mode; hardware path follows the standard Zadig WinUSB +
  librtlsdr DLL stack documented in README

---
Task ID: 3
Agent: main (Super Z)
Task: v1.1 USA edition — pin 75 µs de-emphasis, add RDS station names, NOAA weather presets, airband scanner

Work Log:
- Verified RDS protocol constants against IEC 62106 B.3.1 via the redsea decoder
  source (offset words A=0xFC B=0x198 C=0x168 C'=0x350 D=0x1B4, syndrome consts,
  26x10 parity matrix) + Wikipedia RBDS PTY table; corrected a wrong-from-memory
  C'/D pair. Numerical encode/decode round trips green (scripts/verify_rds.py).
- Built backend/rds.py: 57 kHz DSB-SC extraction (mix -> 2-stage decimation to
  9.6 kSPS), Costas carrier loop + Gardner timing (tail-carry across blocks),
  differential decode, CRC-10 block sync, group parser (PS/PI/PTY/TA/MS/AF/
  RadioText/clock). BlockCoder doubles as encoder for the sim test signal.
- Debug chain: test harness units bug (rad/sample), PM-vs-DSBSC encoder fix,
  RC Nyquist pulse for the sim, Gardner TED sign flip, GF(2) solver replaced by
  brute-force table, periodic-stream differential-wrap corruption fixed by
  encoding 64 cycles linearly.
- dsp.py: PilotWatch (19 kHz Goertzel in the MPX domain -> stereo flag + real
  dongle ppm, feeds RDS NCO), NbfmDemod (5 kHz dev, voice shaping, AGC),
  AmDemod offset-tuned variant for airband, channel-power squelch metric.
- FIXED v1.0 shipping bug: NcoMixer(-lo_offset) shifted the station UP +500 kHz
  (wrong sign) -> FM "audio" had been filtered noise; now NcoMixer(+lo_offset).
  Caught precisely because the RDS pilot never appeared through the app chain.
- BLOCK 262144 -> 262140 (divisible by the x10x5 decimation chain) — killed a
  0.44 Hz block-boundary frequency artifact in the ppm estimator.
- stream.py: WX + AIR bands, channel-power squelch with 0.5 s tail that actually
  gates audio (found + fixed muted-but-not-muted bug in smoke test), scanner
  state machine (hop/hold/release, gen-guarded measurements, activity log,
  manual tune preempts).
- config.py: region US, wx/air bands + WX1-WX7 / key-airband presets, scan
  lists (noaa 7, air_key 10, air_band 760 channels).
- device.py sim: mode-tagged stations (wfm/rds/am/nbfm), 96.1 MHz carries
  pilot+RDS test signal (PI 2A5B, PS SIM-RDS), NOAA voice on 162.400/162.550,
  airband AM on 121.5/122.8/123.1.
- app.py: /api/scan, /api/squelch, scan in /api/status, rds/scan/squelch/chan_db
  piggybacked on /ws/spectrum frames.
- web UI: WX/AIR tabs, preset chips with active highlight, RDS panel (PS amber
  readout, PI/PTY/clock, STEREO/TP/TA/LOCK flags, RadioText, AF list),
  scanner panel (list select, start/stop, SQL slider, live LVL meter, hit log),
  readout follows server-driven tuning.
- Tests: verify_rds (round trips), test_rds_chain (7.9x realtime decode w/ 37 ppm
  offset), test_engine (RDS+squelch+scanner), smoke_test 22/22 PASS over REST+WS,
  headless browser verified FM RDS panel, WX chips, scanner hold + log.
- Packaged rtl-sdr-player.zip (21 files) + fresh UI screenshots in download/.

Stage Summary:
- Deliverable: /home/z/my-project/download/rtl-sdr-player.zip (+ unzipped tree)
- All four requested features shipped and verified end-to-end in sim mode:
  75 us/US pinned (config + RBDS PTY), RDS decode UI, NOAA presets + NBFM,
  airband scanner with squelch. Test scripts kept in /home/z/my-project/scripts/.
