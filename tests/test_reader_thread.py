"""Headless unit tests for the v1.3.5 USB reader thread (stream.py).

Covers, without any real device or threads:
  * short reads accumulate into full blocks (no audio discarded)
  * surplus beyond a full block is kept for the next one
  * empty reads are ignored
  * drop-oldest behavior when the DSP thread falls behind
  * the retune parking handshake (park -> config -> resume)
  * capture stats exposed in engine.status()
"""
import sys
import time

sys.path.insert(0, "/home/z/my-project/download/rtl-sdr-player")

import numpy as np
from backend import config as cfg
from backend import dsp
from backend.stream import ReceiverEngine


class FakeDevice:
    """Returns scripted chunk sizes; records config calls."""

    def __init__(self, chunk_plan):
        self.chunk_plan = list(chunk_plan)   # e.g. [BLOCK//3, BLOCK//3, ...]
        self.reads = 0
        self.gain_calls = []

    def read_iq(self, n):
        self.reads += 1
        if self.chunk_plan:
            k = self.chunk_plan.pop(0)
        else:
            k = n
        k = min(k, n)
        return np.zeros(k, dtype=np.complex64)

    def set_gain(self, g):
        self.gain_calls.append(g)

    def get_gains(self):
        return []


def make_engine():
    eng = ReceiverEngine(dict(cfg.DEFAULTS), prefer_sim=True)
    eng._fs = 2_400_000
    eng._center = 98_050_000
    eng._block_seconds = dsp.BLOCK / 2_400_000
    return eng


fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


print("== short reads accumulate into full blocks ==")
eng = make_engine()
eng.device = FakeDevice([dsp.BLOCK // 3, dsp.BLOCK // 3,
                         dsp.BLOCK // 3, dsp.BLOCK // 2, dsp.BLOCK // 2])
for _ in range(3):
    eng._reader_step()          # 3 x BLOCK/3 -> residue reaches BLOCK on 3rd
check("first full block after 3 short reads",
      len(eng._raw_q) == 1, {"q": len(eng._raw_q), "residue": None if eng._residue is None else len(eng._residue)})
check("all three short reads counted", eng.stats["short_reads"] == 3,
      eng.stats["short_reads"])
eng._reader_step()
eng._reader_step()              # BLOCK/2 + residue(BLOCK/2)? no: fresh half
check("second block emitted from halves, no residue left",
      len(eng._raw_q) == 2 and eng._residue is None,
      {"q": len(eng._raw_q), "residue": None if eng._residue is None else len(eng._residue)})

print("== residue carries into the next full block (surplus kept) ==")
eng = make_engine()
eng.device = FakeDevice([500, dsp.BLOCK])
eng._reader_step()
check("tiny read -> residue only", len(eng._raw_q) == 0
      and eng._residue is not None and len(eng._residue) == 500)
eng._reader_step()
check("500 + full read -> one block + 500 residue",
      len(eng._raw_q) == 1 and eng._residue is not None
      and len(eng._residue) == 500)
eng.device.chunk_plan = [dsp.BLOCK]
eng._reader_step()
check("still one residue chunk after next read",
      len(eng._raw_q) == 2 and eng._residue is not None
      and len(eng._residue) == 500)

print("== empty read ignored ==")
eng = make_engine()
eng.device = FakeDevice([0, dsp.BLOCK])
eng._reader_step()
eng._reader_step()
check("empty read skipped, next full read queued",
      len(eng._raw_q) == 1 and len(eng._raw_q[0]) == dsp.BLOCK)

print("== drop-oldest when DSP falls behind ==")
eng = make_engine()
eng.device = FakeDevice([])
full = np.zeros(dsp.BLOCK, dtype=np.complex64)
for _ in range(eng._raw_max + 3):
    eng._put_block(full)
check("queue capped at _raw_max", len(eng._raw_q) == eng._raw_max)
check("overflows counted", eng.stats["usb_overflows"] == 3,
      eng.stats["usb_overflows"])

print("== retune parking handshake ==")
eng = make_engine()
eng.device = FakeDevice([])
eng._pause_req.set()
eng._reader_step()
check("reader parks when asked", eng._parked.is_set()
      and eng.device.reads == 0)
eng._pause_req.clear()
eng._resume.set()
eng._reader_step()
check("reader resumes and reads one block", eng.device.reads == 1
      and len(eng._raw_q) == 1)

print("== capture stats in status() ==")
eng = make_engine()
eng.device = FakeDevice([])
s = eng.status()
cap = s.get("capture") or {}
check("status has capture dict",
      all(k in cap for k in ("usb_overflows", "short_reads", "dsp_ms",
                             "queue_blocks")), cap)

print("== threaded smoke: sim engine realtime for 3 s ==")
eng2 = ReceiverEngine(dict(cfg.DEFAULTS), prefer_sim=True)
eng2.start()
sub = eng2.subscribe()
total = 0
t_end = time.monotonic() + 3.0
while time.monotonic() < t_end:          # drain continuously like the WS layer
    time.sleep(0.5)
    total += sum(len(c) for c in eng2.drain_sub(sub))
chunks = eng2.drain_sub(sub)
total += sum(len(c) for c in chunks)
secs = total / 48000.0
eng2.unsubscribe(sub)
eng2.close()
check("3 s wall -> ~3 s of audio delivered", 2.4 < secs < 4.0, f"{secs:.2f} s")
check("threads joined", not eng2._thread.is_alive() and not eng2._reader.is_alive())
check("sim reader paced (no overflow storm)",
      eng2.stats["usb_overflows"] < 5, eng2.stats["usb_overflows"])

print()
if fails:
    print("FAILED:", fails)
    raise SystemExit(1)
print("READER-THREAD TESTS: ALL PASS")
