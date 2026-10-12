/* PCM player AudioWorklet — jitter-buffered mono playback.
 *
 * Receives Float32Array chunks (48 kHz, mono) over the port, fills a ring
 * buffer, and only starts pulling audio out once the buffer reaches the
 * target level (~2.5 s by default). Short network hiccups ride through
 * undetected.
 *
 * v1.7.1 audio-quality engine:
 *   - starvation recovery: instead of resuming with a few ms of audio
 *     (rapid start/stop stutter), playback re-primes to an adaptive resume
 *     level (150 ms -> doubles per repeat, caps at 2 s, relaxes back after
 *     30 healthy seconds) before it comes back
 *   - click-free edges: a pre-underrun ramp fades the tail out as the
 *     buffer drains, and every (re)start gets a short fade-in
 *   - volume ramps: slider changes glide across each block instead of
 *     stepping (no zipper noise)
 *   - clock-drift servo: the dongle clock and the sound-card clock differ
 *     by tens-to-hundreds of ppm; the read step is nudged by up to +/-300
 *     ppm to keep the buffer centred (a <=0.03% pitch shift is inaudible)
 */
class PcmPlayer extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{
      name: "volume", defaultValue: 0.7, minValue: 0, maxValue: 2,
      automationRate: "k-rate",
    }];
  }

  constructor() {
    super();
    const seconds = 8;
    this.capacity = Math.ceil(sampleRate * seconds);
    this.ring = new Float32Array(this.capacity);
    this.writeIdx = 0;
    this.readPos = 0;          // fractional read position (in source samples)
    this.fill = 0;             // buffered samples
    this.started = false;
    this.targetMs = 2500;
    this.ratio = 1;            // src(48k) -> ctx rate resampling step (base)
    this.ratioEff = 1;         // ratio * drift-servo correction
    this.lastStats = 0;
    // volume: volTgt is what the UI asked for, volCur glides toward it
    this.volume = null;        // set via {type:"vol"}; legacy passthrough
    this.volCur = 0;
    this.volTgt = 0;
    // starvation / edges
    this.underruns = 0;        // diagnostic: playback starved while streaming
    this.starved = false;      // true while waiting to re-prime after a starve
    this.resumeMs = 150;       // re-prime level after a starvation (adaptive)
    this.lastStarveAt = -1e9;
    this.fadeSamples = Math.max(64, Math.round(sampleRate * 0.008));
    this.fadeIn = 0;           // counts down while fading in after (re)start
    this.port.onmessage = (e) => this.onMessage(e.data);
  }

  onMessage(msg) {
    if (msg.type === "pcm") {
      const data = msg.data;
      for (let i = 0; i < data.length; i++) {
        this.ring[this.writeIdx] = data[i];
        this.writeIdx = (this.writeIdx + 1) % this.capacity;
      }
      this.fill += data.length;
    } else if (msg.type === "flush") {
      // retune / band switch: full re-prime, like a fresh start
      this.writeIdx = 0; this.readPos = 0; this.fill = 0;
      this.started = false; this.starved = false; this.fadeIn = 0;
    } else if (msg.type === "cfg") {
      if (msg.targetMs) this.targetMs = msg.targetMs;
      if (msg.ratio) { this.ratio = msg.ratio; this.ratioEff = msg.ratio; }
    } else if (msg.type === "vol") {
      // NOTE: AudioWorkletProcessor has no this.parameters — the k-rate
      // "volume" param is only readable inside process(). Store it here
      // and let process() apply it (pre-1.3.3 this threw a TypeError).
      this.volTgt = msg.v;
      this.volume = msg.v;
    } else if (msg.type === "query") {
      this.port.postMessage({ type: "stats", fillMs: (this.fill / sampleRate) * 1000 });
    }
  }

  process(inputs, outputs, parameters) {
    const out = outputs[0][0];
    if (!out) return true;
    const n = out.length;
    const volTgt = (this.volume !== null && this.volume !== undefined)
      ? this.volume : parameters.volume[0];
    this.volTgt = volTgt;
    const target = Math.floor((this.targetMs / 1000) * sampleRate);
    const resume = Math.min(
      Math.floor((this.resumeMs / 1000) * sampleRate), target);

    // ---- starvation at block start: stop, count, pick a re-prime level
    if (this.started && this.fill <= 1) {
      this.underruns++;
      this.starved = true;
      this.started = false;
      this.lastStarveAt = currentTime;
      this.resumeMs = Math.min(this.resumeMs * 2, 2000);
    }

    // ---- (re)start when enough jitter-buffer depth is available.
    // First prime waits for the full target; a starvation recovery only
    // waits for the adaptive resume level, so recovery is quick but solid.
    if (!this.started) {
      const need = this.starved ? resume : target;
      if (this.fill >= need) {
        this.started = true;
        this.fadeIn = this.fadeSamples;
      }
    }

    let i = 0;
    if (this.started) {
      const fadeSpan = this.fadeSamples;
      for (; i < n; i++) {
        if (this.fill <= 1) break;          // ran dry mid-block
        const r = Math.floor(this.readPos);
        const frac = this.readPos - r;
        const i0 = (this.writeIdx - this.fill + r + this.capacity) % this.capacity;
        const i1 = (i0 + 1) % this.capacity;
        const s = this.ring[i0] + frac * (this.ring[i1] - this.ring[i0]);
        // edge envelope: fade-in after (re)start + pre-underrun fade-out
        let env = 1.0;
        if (this.fadeIn > 0) {
          env = Math.min(env, 1 - this.fadeIn / fadeSpan);
          this.fadeIn--;
        }
        const remain = this.fill - 1 - r;
        if (remain < fadeSpan) {
          env = Math.min(env, Math.max(0, remain) / fadeSpan);
        }
        // volume glide across the block (zipper-noise free)
        const vol = this.volCur + (this.volTgt - this.volCur) * (i + 1) / n;
        out[i] = vol * env * s;
        this.readPos += this.ratioEff;
        const adv = Math.floor(this.readPos);
        if (adv > 0) {
          this.readPos -= adv;
          this.fill -= adv;
        }
      }
    }
    if (i < n) {                            // silence for the rest of the block
      for (let j = i; j < n; j++) out[j] = 0;
    }
    if (this.started && i >= n) {
      this.volCur = this.volTgt;            // glide finished this block
      this.starved = false;
    } else if (i < n && this.started) {
      // mid-block starve: enter recovery exactly like a block-edge starve
      this.underruns++;
      this.starved = true;
      this.started = false;
      this.lastStarveAt = currentTime;
      this.resumeMs = Math.min(this.resumeMs * 2, 2000);
    } else {
      this.volCur = this.volTgt;            // silent while priming: no glide
    }

    // ---- stats + drift servo tick (runs even while silent: the UI shows
    // the buffer filling during the initial prime)
    const now = currentTime * 1000;
    if (now - this.lastStats > 300) {
      this.lastStats = now;
      const errS = (this.fill - target) / sampleRate;
      // 1 s of buffer error -> ~1000 ppm pull, clamped to +/-300 ppm
      const corr = Math.max(-3e-4, Math.min(3e-4, errS * 1e-3));
      this.ratioEff = this.ratio * (1 + corr);
      // long healthy stretch: relax the re-prime level back toward 150 ms
      if (!this.starved && currentTime - this.lastStarveAt > 30
          && this.resumeMs > 150) {
        this.resumeMs = Math.max(150, this.resumeMs * 0.75);
      }
      this.port.postMessage({ type: "stats", fillMs: (this.fill / sampleRate) * 1000,
                              underruns: this.underruns });
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
