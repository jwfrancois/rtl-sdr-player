/* PCM player AudioWorklet — jitter-buffered mono playback.
 *
 * Receives Float32Array chunks (48 kHz, mono) over the port, fills a ring
 * buffer, and only starts pulling audio out once the buffer reaches the
 * target level (~2.5 s by default). This gives the "smooth" streaming
 * behaviour: short network hiccups ride through undetected.
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
    this.readPos = 0;          // fractional read position
    this.fill = 0;             // buffered samples
    this.started = false;
    this.targetMs = 2500;
    this.ratio = 1;            // src(48k) -> ctx rate resampling step
    this.carry = 0;
    this.lastStats = 0;
    this.volume = null;        // set via {type:"vol"}; falls back to the AudioParam
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
      this.writeIdx = 0; this.readPos = 0; this.fill = 0; this.started = false;
    } else if (msg.type === "cfg") {
      if (msg.targetMs) this.targetMs = msg.targetMs;
      if (msg.ratio) this.ratio = msg.ratio;
    } else if (msg.type === "vol") {
      // NOTE: AudioWorkletProcessor has no this.parameters — the k-rate
      // "volume" param is only readable inside process(). Store it here
      // and let process() apply it (pre-1.3.3 this threw a TypeError).
      this.volume = msg.v;
    } else if (msg.type === "query") {
      this.port.postMessage({ type: "stats", fillMs: (this.fill / sampleRate) * 1000 });
    }
  }

  process(inputs, outputs, parameters) {
    const out = outputs[0][0];
    if (!out) return true;
    const vol = (this.volume !== null && this.volume !== undefined)
      ? this.volume : parameters.volume[0];
    const target = Math.floor((this.targetMs / 1000) * sampleRate);

    if (!this.started) {
      if (this.fill >= target) this.started = true;
    }

    const n = out.length;
    if (!this.started || this.fill <= 1) {
      out.fill(0);
    } else {
      for (let i = 0; i < n; i++) {
        // advance the fractional read head; stop at end of buffer
        if (this.readPos > this.fill - 1) this.readPos = Math.max(0, this.fill - 1);
        let avail = this.fill - 1;
        if (avail <= 0) { out[i] = 0; continue; }
        const r = Math.floor(this.readPos);
        const frac = this.readPos - r;
        const i0 = (this.writeIdx - this.fill + r + this.capacity) % this.capacity;
        const i1 = (i0 + 1) % this.capacity;
        out[i] = vol * (this.ring[i0] + frac * (this.ring[i1] - this.ring[i0]));
        this.readPos += this.ratio;
        const adv = Math.floor(this.readPos);
        if (adv > 0) {
          this.readPos -= adv;
          this.fill -= adv;
        }
      }
    }

    const now = currentTime * 1000;
    if (now - this.lastStats > 300) {
      this.lastStats = now;
      this.port.postMessage({ type: "stats", fillMs: (this.fill / sampleRate) * 1000 });
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
