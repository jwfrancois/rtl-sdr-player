/* RTL-SDR Player — frontend application */
"use strict";

// ---------------------------------------------------------------- state
const S = {
  band: "wfm",
  freq: 98300000,
  psd: null,
  gains: [],
  deviceMode: null,
  targetMs: 2500,
  wsAudio: null,
  wsSpectrum: null,
  audioSeq: 0,
  specSeq: 0,
  actx: null,
  node: null,
  volume: 0.7,
  dragThrottle: 0,
  synced: false,
  presets: null,
  scanRunning: false,
  squelchTouched: false,
};

const $ = (id) => document.getElementById(id);
const FMT = {
  wfm: { div: 1e6, dec: 2, unit: "MHz", grid: 200e3, axis: (hz) => (hz / 1e6).toFixed(1) + "M" },
  am:  { div: 1e3, dec: 0, unit: "kHz", grid: 100e3, axis: (hz) => (hz / 1e3).toFixed(0) + "k" },
  wx:  { div: 1e6, dec: 3, unit: "MHz", grid: 25e3, axis: (hz) => (hz / 1e6).toFixed(3) + "M" },
  air: { div: 1e6, dec: 3, unit: "MHz", grid: 1e6, axis: (hz) => (hz / 1e6).toFixed(3) + "M" },
};
const STEP = { wfm: 100e3, am: 10e3, wx: 25e3, air: 25e3 };
const BAND_DEFAULT = { wfm: 98.3e6, am: 990e3, wx: 162.55e6, air: 121.5e6 };
const fmtFreq = (hz) => (hz / FMT[S.band].div).toFixed(FMT[S.band].dec);

// ---------------------------------------------------------------- boot
window.addEventListener("DOMContentLoaded", boot);

async function boot() {
  wireControls();
  updateReadout(S.freq);
  connectSockets();          // spectrum/status must not depend on audio
  initAudio().catch((e) => console.warn("audio init deferred:", e));
  pollStatus();
}

async function refreshStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    applyStatus(st);
    return st;
  } catch { return null; }
}

function applyStatus(st) {
  $("app-version").textContent = "v" + st.version;
  const dot = $("status-dot");
  dot.className = "dot " + (st.mode === "hardware" ? "hw" : "sim");
  $("device-name").textContent = st.device || "unknown device";
  S.deviceMode = st.mode;
  S.gains = st.gains || [];
  S.targetMs = st.target_buffer_ms || 2500;
  S.presets = {};
  for (const [key, b] of Object.entries(st.bands || {})) {
    S.presets[key] = b.presets || [];
  }
  if (S.node) S.node.port.postMessage({ type: "cfg", targetMs: S.targetMs });

  // first sync: adopt the server's band/frequency as the UI starting point
  if (!S.synced && st.freq_hz) {
    S.synced = true;
    S.band = st.band || S.band;
    S.freq = st.freq_hz;
    document.querySelectorAll(".tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.band === S.band));
    updateReadout(S.freq);
    renderPresets();
    updatePanelVisibility();
  }

  // populate gain list once
  const sel = $("gain-select");
  if (sel.options.length <= 1 && S.gains.length) {
    for (const g of S.gains) {
      const opt = document.createElement("option");
      opt.value = g; opt.textContent = g.toFixed(1) + " dB";
      sel.appendChild(opt);
    }
  }
  $("stat-note").textContent = st.error ? ("⚠ " + st.error) :
    (st.note || "");
  $("band-desc").textContent = (st.bands?.[S.band]?.desc) || "";
  const deemph = st.de_emphasis_us || 75;
  const region = st.region || "US";
  const demodLines = {
    wfm: `WFM mono · offset-tuned · de-emphasis ${deemph} µs (${region}) · RDS/RBDS`,
    wx: "NBFM 5 kHz dev · NOAA Weather Radio · offset-tuned",
    air: "AM envelope · US airband 25 kHz channels · offset-tuned",
    am: "AM envelope · direct-sampling (Q input)",
  };
  $("demod-line").textContent =
    (demodLines[S.band] || S.band) + " · 2.4 MSPS capture";
  $("dn").title = "-" + (STEP[S.band] / 1e3).toFixed(0) + " kHz";
  $("up").title = "+" + (STEP[S.band] / 1e3).toFixed(0) + " kHz";

  // scanner list options (once)
  const listSel = $("scan-list");
  if (listSel.options.length === 0 && st.scan_lists) {
    for (const [key, label] of Object.entries(st.scan_lists)) {
      const opt = document.createElement("option");
      opt.value = key; opt.textContent = label;
      listSel.appendChild(opt);
    }
  }
  if (st.squelch_db != null && !S.squelchTouched) {
    $("squelch").value = st.squelch_db;
    $("squelch-val").textContent = st.squelch_db.toFixed(0) + " dB";
  }
  $("stat-buffer").textContent = S.node
    ? $("stat-buffer").textContent  // live reading comes from the worklet
    : (S.targetMs / 1000).toFixed(1) + " s target";
}

async function pollStatus() {
  await refreshStatus();
  setInterval(refreshStatus, 8000);
}

// ---------------------------------------------------------------- controls
function wireControls() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchBand(btn.dataset.band));
  });
  $("dn").onclick = () => nudge(-1);
  $("up").onclick = () => nudge(+1);
  $("dn-big").onclick = () => nudge(-10);
  $("up-big").onclick = () => nudge(+10);
  $("freq-go").onclick = submitFreq;
  $("freq-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitFreq();
  });
  $("volume").addEventListener("input", (e) => {
    S.volume = e.target.value / 100;
    if (S.node) S.node.port.postMessage({ type: "vol", v: S.volume });
  });
  $("gain-select").addEventListener("change", (e) => {
    const v = e.target.value;
    fetch("/api/gain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gain_db: v === "auto" ? null : parseFloat(v) }),
    });
  });

  // scanner controls
  $("scan-btn").onclick = () => {
    if (S.scanRunning) {
      fetch("/api/scan", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "stop" }) });
    } else {
      fetch("/api/scan", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "start",
                               list: $("scan-list").value,
                               squelch_db: parseFloat($("squelch").value) }) });
    }
  };
  let sqTimer = null;
  $("squelch").addEventListener("input", (e) => {
    S.squelchTouched = true;
    const db = parseFloat(e.target.value);
    $("squelch-val").textContent = db.toFixed(0) + " dB";
    clearTimeout(sqTimer);
    sqTimer = setTimeout(() => {
      fetch("/api/squelch", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ db }) });
    }, 200);
  });

  const spec = $("spectrum");
  spec.addEventListener("pointerdown", (e) => {
    spec.setPointerCapture(e.pointerId);
    tuneFromPixel(e, true);
    spec.onpointermove = (ev) => tuneFromPixel(ev, false);
    spec.onpointerup = () => { spec.onpointermove = null; };
  });
}

function switchBand(band) {
  const prevBand = S.band;
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.band === band));
  S.band = band;
  $("radio-controls").style.visibility = band === "tv" ? "hidden" : "visible";
  $("view-spectrum").hidden = band === "tv";
  $("view-tv").hidden = band !== "tv";
  renderPresets();
  updatePanelVisibility();
  if (band === "tv") { renderTv(); return; }
  tuneTo(band, band !== prevBand ? BAND_DEFAULT[band] : S.freq);
}

function renderPresets() {
  const wrap = $("presets");
  wrap.innerHTML = "";
  const presets = (S.presets && S.presets[S.band]) || [];
  for (const p of presets) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.textContent = p.label;
    btn.onclick = () => tuneTo(S.band, p.hz);
    wrap.appendChild(btn);
  }
}

function updatePanelVisibility() {
  $("rds-panel").hidden = S.band !== "wfm";
  $("scan-panel").hidden = !(S.band === "air" || S.band === "wx");
}

function nudge(mult) {
  tuneTo(S.band, S.freq + (STEP[S.band] || 100e3) * mult);
}

function submitFreq() {
  const raw = parseFloat($("freq-input").value);
  if (isNaN(raw)) return;
  const hz = S.band === "am" ? raw * 1e3 : raw * 1e6;
  tuneTo(S.band, hz);
}

async function tuneTo(band, freq, force) {
  S.band = band;
  updateReadout(freq);
  const res = await fetch("/api/tune", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ band, freq_hz: Math.round(freq) }),
  }).then((r) => r.json()).catch(() => null);
  if (res && res.ok) {
    S.freq = res.freq_hz;
    updateReadout(res.freq_hz);
    flushAudio();
  }
  refreshStatus();
}

function nudgeNoop() {}
function updateReadout(freq) {
  S.freq = freq;
  $("freq-display").textContent = fmtFreq(freq);
  $("freq-unit").textContent = FMT[S.band].unit;
  $("freq-input").value = fmtFreq(freq);
  document.querySelectorAll("#presets .chip").forEach((c) =>
    c.classList.remove("active"));
  const presets = (S.presets && S.presets[S.band]) || [];
  const hit = presets.findIndex((p) => Math.abs(p.hz - freq) < 500);
  if (hit >= 0) {
    const chips = document.querySelectorAll("#presets .chip");
    if (chips[hit]) chips[hit].classList.add("active");
  }
}

function tuneFromPixel(ev, immediate) {
  const now = performance.now();
  if (!immediate && now - S.dragThrottle < 180) return;
  S.dragThrottle = now;
  if (!S.psd) return;
  const rect = ev.target.getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  const off = S.psd.axis_hz0 + frac * (S.psd.axis_hz1 - S.psd.axis_hz0);
  const target = Math.round(S.psd.center_hz + off);
  const step = STEP[S.band] || 100e3;
  const snapped = Math.round(target / step) * step;
  updateReadout(snapped);
  tuneTo(S.band, snapped);
}

// ---------------------------------------------------------------- audio
async function initAudio() {
  try {
    S.actx = new AudioContext({ sampleRate: 48000, latencyHint: "playback" });
  } catch {
    S.actx = new AudioContext();  // old browsers: resample inside the worklet
  }
  // Browsers suspend AudioContext until a user gesture; retry on first click.
  if (S.actx.state === "suspended") {
    const resume = () => S.actx.resume().catch(() => {});
    document.addEventListener("pointerdown", resume, { once: true });
  }
  const ratio = 48000 / S.actx.sampleRate;
  const url = new URL("worklet.js", location.href);
  await S.actx.audioWorklet.addModule(url);
  S.node = new AudioWorkletNode(S.actx, "pcm-player", {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
  });
  S.node.port.onmessage = (e) => {
    if (e.data.type === "stats") {
      $("stat-buffer").textContent =
        (e.data.fillMs / 1000).toFixed(1) + " s / " + (S.targetMs / 1000).toFixed(1) + " s";
    }
  };
  S.node.port.postMessage({ type: "cfg", targetMs: S.targetMs, ratio });
  S.node.port.postMessage({ type: "vol", v: S.volume });
  S.node.connect(S.actx.destination);
}

function flushAudio() {
  if (S.node) S.node.port.postMessage({ type: "flush" });
}

// ---------------------------------------------------------------- sockets
function connectSockets() {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  connectAudio(proto);
  connectSpectrum(proto);
}

function connectAudio(proto) {
  const seq = ++S.audioSeq;
  const ws = new WebSocket(proto + location.host + "/ws/audio");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    $("stat-ws").textContent = "connected";
    $("stat-ws").className = "good";
  };
  ws.onmessage = (msg) => {
    if (typeof msg.data === "string") {
      const ev = JSON.parse(msg.data);
      if (ev.event === "reset") flushAudio();
      return;
    }
    if (!S.node) return;
    const i16 = new Int16Array(msg.data);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    S.node.port.postMessage({ type: "pcm", data: f32 }, [f32.buffer]);
  };
  ws.onclose = () => {
    if (seq !== S.audioSeq) return;  // superseded by a newer connection
    $("stat-ws").textContent = "reconnecting…";
    $("stat-ws").className = "bad";
    setTimeout(() => connectAudio(proto), 2500);
  };
  S.wsAudio = ws;
}

function connectSpectrum(proto) {
  const seq = ++S.specSeq;
  const ws = new WebSocket(proto + location.host + "/ws/spectrum");
  ws.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.event === "psd") {
      S.psd = ev;
      drawSpectrum();
      drawWaterfall();
      drawAxis(ev);
      updateMeter(ev);
      updateRds(ev);
      updateScan(ev);
      if (ev.band === S.band && Math.abs(ev.freq_hz - S.freq) > 500) {
        updateReadout(ev.freq_hz);   // server-driven change (e.g. scanner)
      }
      $("scope-info").textContent =
        (ev.span_hz / 1e6).toFixed(2) + " MHz span · center " +
        (ev.center_hz / 1e6).toFixed(3) + " MHz";
    }
  };
  ws.onclose = () => {
    if (seq !== S.specSeq) return;
    setTimeout(() => connectSpectrum(proto), 2500);
  };
  S.wsSpectrum = ws;
}

// ---------------------------------------------------------------- meter
function updateMeter(psd) {
  const snr = psd.sig_db - psd.floor_db;
  const pct = Math.max(0, Math.min(100, ((snr - 3) / 35) * 100));
  $("meter-fill").style.width = pct.toFixed(0) + "%";
  $("meter-db").textContent = snr.toFixed(1) + " dB";
}

// ---------------------------------------------------------------- RDS panel
function updateRds(ev) {
  if (S.band !== "wfm" || !ev.rds) return;
  const r = ev.rds;
  $("rds-ps").textContent = r.ps || "\u2014";
  const flags = [];
  flags.push(r.pilot ? '<span class="on">STEREO</span>' : "mono");
  if (r.tp) flags.push('<span class="on">TP</span>');
  if (r.ta) flags.push('<span class="on">TA</span>');
  if (r.locked && r.pilot) flags.push('<span class="on">LOCK</span>');
  $("rds-flags").innerHTML = flags.join(" \u00b7 ");
  if (!r.pilot) {
    $("rds-meta").textContent = "no pilot \u00b7 no RDS on this signal";
  } else if (!r.ps) {
    $("rds-meta").textContent =
      "pilot " + (r.pilot_db == null ? "?" : r.pilot_db.toFixed(0)) +
      " dB \u00b7 decoding\u2026 (" + r.groups + " groups)";
  } else {
    const bits = ["PI " + (r.pi || "\u2014"), r.pty || ""];
    if (r.clock) bits.push(r.clock);
    $("rds-meta").textContent = bits.filter(Boolean).join(" \u00b7 ");
  }
  $("rds-rt").textContent = r.radiotext || "";
  $("rds-af").textContent =
    r.af_mhz && r.af_mhz.length
      ? "AF: " + r.af_mhz.map((f) => f.toFixed(1)).join("  ") + " MHz"
      : "";
}

// ------------------------------------------------------------ scanner panel
function updateScan(ev) {
  const sc = ev.scan;
  if (!sc) return;
  S.scanRunning = !!sc.running;
  const stateEl = $("scan-state");
  if (!sc.running && sc.list == null) {
    stateEl.textContent = "idle";
    stateEl.className = "scan-state";
  } else if (sc.running) {
    stateEl.textContent = sc.state === "hold"
      ? "HOLD " + (sc.freq_hz / 1e6).toFixed(3)
      : "scanning " + (sc.idx + 1) + "/" + sc.count;
    stateEl.className = "scan-state " + (sc.state === "hold" ? "hold" : "run");
    if ((S.band === "wx" || S.band === "air") &&
        Math.abs(sc.freq_hz - S.freq) > 500) {
      updateReadout(sc.freq_hz);
    }
  } else {
    stateEl.textContent = "stopped";
    stateEl.className = "scan-state";
  }
  $("scan-btn").textContent = sc.running ? "Stop" : "Scan";

  if (ev.chan_db != null) {
    const pct = Math.max(0, Math.min(100, ((ev.chan_db - -60) / 55) * 100));
    const fill = $("chan-fill");
    fill.style.width = pct.toFixed(0) + "%";
    fill.style.background = ev.squelch_open
      ? "linear-gradient(90deg, #0e7490, #34d399)"
      : "linear-gradient(90deg, #0e7490, #22d3ee)";
    $("chan-val").textContent = ev.chan_db.toFixed(0) + " dB" +
      (ev.squelch_open ? " open" : "");
  }

  const log = $("scan-log");
  if (sc.log && sc.log.length) {
    log.innerHTML = sc.log.slice(0, 4).map((e) => {
      const t = new Date(e.t * 1000).toLocaleTimeString([],
        { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      return '<div class="hit">' + t + "  " +
        (e.hz / 1e6).toFixed(3) + " MHz</div>";
    }).join("");
  } else if (!sc.running) {
    log.innerHTML = "<div class='empty'>no activity logged yet</div>";
  }
}

// ---------------------------------------------------------------- spectrum
const specCv = $("spectrum");
const wfCv = $("waterfall");
const wfOff = document.createElement("canvas");
wfOff.width = 512; wfOff.height = 256;
const wfCtx = wfOff.getContext("2d");
const wfTmp = document.createElement("canvas");
wfTmp.width = 512; wfTmp.height = 1;
const wfTmpCtx = wfTmp.getContext("2d");

// colormap: dark navy -> teal -> cyan -> yellow-green -> white
const LUT = (() => {
  const stops = [
    [0, [4, 12, 24]], [0.25, [11, 57, 88]], [0.5, [14, 142, 168]],
    [0.7, [57, 210, 160]], [0.85, [242, 227, 90]], [1, [255, 255, 255]],
  ];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let a = stops[0], b = stops[stops.length - 1];
    for (let s = 0; s < stops.length - 1; s++) {
      if (t >= stops[s][0] && t <= stops[s + 1][0]) { a = stops[s]; b = stops[s + 1]; break; }
    }
    const f = (t - a[0]) / (b[0] - a[0] || 1);
    for (let c = 0; c < 3; c++)
      lut[i * 3 + c] = a[1][c] + f * (b[1][c] - a[1][c]);
  }
  return lut;
})();

function sizeCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== w * dpr || cv.height !== h * dpr) {
    cv.width = w * dpr; cv.height = h * dpr;
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

function drawSpectrum() {
  const ctx = sizeCanvas(specCv);
  const w = specCv.clientWidth, h = specCv.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#060a0e";
  ctx.fillRect(0, 0, w, h);

  // grid
  ctx.strokeStyle = "#0f1a24";
  ctx.lineWidth = 1;
  for (let i = 1; i < 6; i++) {
    const y = (h / 6) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  if (!S.psd) {
    ctx.fillStyle = "#3a4a5a";
    ctx.font = "12px " + getComputedStyle(document.body).fontFamily;
    ctx.fillText("waiting for spectrum data…", 16, 24);
    return;
  }

  const psd = S.psd.psd_db;
  const top = psd_floor() + 52, bot = psd_floor() - 6;
  const yOf = (db) => h - ((db - bot) / (top - bot)) * h;
  const xOf = (i) => (i / (psd.length - 1)) * w;

  // grid at band steps
  ctx.strokeStyle = "#101c26";
  const span = S.psd.axis_hz1 - S.psd.axis_hz0;
  const gridStep = FMT[S.band].grid;
  const first = Math.ceil((S.psd.center_hz + S.psd.axis_hz0) / gridStep) * gridStep;
  for (let f = first; f < S.psd.center_hz + S.psd.axis_hz1; f += gridStep) {
    const x = ((f - S.psd.center_hz - S.psd.axis_hz0) / span) * w;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }

  // fill under curve
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(34,211,238,0.28)");
  grad.addColorStop(1, "rgba(34,211,238,0.02)");
  ctx.beginPath();
  ctx.moveTo(0, h);
  for (let i = 0; i < psd.length; i++) ctx.lineTo(xOf(i), yOf(psd[i]));
  ctx.lineTo(w, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // trace
  ctx.beginPath();
  for (let i = 0; i < psd.length; i++) {
    const x = xOf(i), y = yOf(psd[i]);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.strokeStyle = "#22d3ee";
  ctx.lineWidth = 1.4;
  ctx.stroke();

  // tuned-frequency marker
  const off = S.freq - S.psd.center_hz;
  if (off >= S.psd.axis_hz0 && off <= S.psd.axis_hz1) {
    const x = ((off - S.psd.axis_hz0) / span) * w;
    ctx.strokeStyle = "#fbbf24";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#fbbf24";
    ctx.beginPath();
    ctx.moveTo(x - 5, 0); ctx.lineTo(x + 5, 0); ctx.lineTo(x, 7);
    ctx.closePath(); ctx.fill();
  }
}

function psd_floor() {
  return S.psd && isFinite(S.psd.floor_db) ? S.psd.floor_db : -60;
}

function drawWaterfall() {
  if (!S.psd) return;
  // scroll existing rows down by 1
  wfCtx.drawImage(wfOff, 0, 1);
  const psd = S.psd.psd_db;
  const img = wfCtx.createImageData(512, 1);
  const floor = psd_floor() - 6, span = 52;
  for (let i = 0; i < 512; i++) {
    const idx = Math.min(psd.length - 1, Math.floor((i / 512) * psd.length));
    const t = Math.max(0, Math.min(1, (psd[idx] - floor) / span));
    const li = Math.floor(t * 255) * 3;
    img.data[i * 4] = LUT[li];
    img.data[i * 4 + 1] = LUT[li + 1];
    img.data[i * 4 + 2] = LUT[li + 2];
    img.data[i * 4 + 3] = 255;
  }
  // put new row at top (temporary canvas avoids drawImage self-overlap)
  wfTmpCtx.putImageData(img, 0, 0);
  wfCtx.drawImage(wfTmp, 0, 0);

  const ctx = sizeCanvas(wfCv);
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(wfOff, 0, 0, wfCv.clientWidth, wfCv.clientHeight);
}

function drawAxis(psd) {
  const el = $("freq-axis");
  const lo = psd.center_hz + psd.axis_hz0;
  const hi = psd.center_hz + psd.axis_hz1;
  let html = "";
  for (let i = 0; i <= 4; i++) {
    html += `<span>${FMT[S.band].axis(lo + ((hi - lo) * i) / 4)}</span>`;
  }
  el.innerHTML = html;
}

// ---------------------------------------------------------------- TV
async function renderTv() {
  const body = $("tv-body");
  body.innerHTML = "<div class='tv-note'>Loading TV capability…</div>";
  let data;
  try {
    data = await (await fetch("/api/tv/status")).json();
  } catch {
    body.innerHTML = "<div class='tv-reason'>Could not reach the server.</div>";
    return;
  }
  const tv = data.status;
  $("tv-badge").textContent = tv.available
    ? "AVAILABLE" : "NOT AVAILABLE ON THIS PLATFORM";
  $("tv-badge").className = "scope-info " + (tv.available ? "tv-badge-ok" : "tv-badge-no");

  let html = "";
  if (tv.reason) html += `<div class="tv-reason">${tv.reason}</div>`;
  html += "<ol class='tv-guide'>" +
    tv.guide.map((g) => `<li>${g}</li>`).join("") + "</ol>";

  if (tv.available) {
    html += `<button class="tv-scan-btn" id="tv-scan">Scan for channels</button>`;
    if (data.scan.running) html += `<div class="tv-note">${data.scan.message}</div>`;
    if (data.channels.length) {
      html += "<div class='tv-channels'>" + data.channels.map(
        (c) => `<button class="tv-chan" data-name="${c.name.replace(/"/g, "&quot;")}">${c.name}</button>`
      ).join("") + "</div>";
      html += "<video class='tv-video' id='tv-video' controls autoplay></video>";
    } else if (!data.scan.running) {
      html += "<div class='tv-note'>No channel list yet — run a scan. " +
        "It saves to ~/.config/dvb/channels.conf.</div>";
    }
  }
  body.innerHTML = html;

  const scanBtn = $("tv-scan");
  if (scanBtn) scanBtn.onclick = async () => {
    scanBtn.disabled = true; scanBtn.textContent = "Scanning…";
    await fetch("/api/tv/scan", { method: "POST" });
    setTimeout(renderTv, 1500);
  };
  body.querySelectorAll(".tv-chan").forEach((btn) => {
    btn.onclick = async () => {
      body.querySelectorAll(".tv-chan").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const res = await (await fetch("/api/tv/watch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: btn.dataset.name }),
      })).json();
      if (res.ok) {
        const v = $("tv-video");
        if (v) v.src = "/api/tv/stream";
      }
    };
  });
}
