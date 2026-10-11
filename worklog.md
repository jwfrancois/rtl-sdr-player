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
