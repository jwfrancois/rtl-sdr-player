"""Scheduled-sweep change digest (v1.7).

Every measurable event of the receiver lands in a small rolling journal:
sweeps (scheduled or manual), preset-bank changes (auto-fill, PI lookup
renames, AI naming, manual edits) and RDS audits. Once a day — at a
configurable local hour — the journal is compiled into a plain-text
digest, written to <data>/state/digests/YYYY-MM-DD.txt and, when SMTP
settings are configured, emailed.

Design rules that mirror the rest of the backend:
  * stdlib only (smtplib + email) — no new pip dependencies
  * every function degrades gracefully; a failed email or a missing
    journal is data in the returned note, never an exception
  * all writes are atomic (tmp + os.replace) and bounded (the journal
    prunes itself to DIGEST_KEEP_DAYS)

The engine (stream.py) owns the scheduler tick and the agent settings;
this module is deliberately engine-free so it stays unit-testable.
"""
import json
import os
import smtplib
import socket
import time
from email.message import EmailMessage
from email.utils import formatdate

from . import config as cfg

JOURNAL_FILE = "digest_journal.json"
DIGESTS_SUBDIR = "digests"


# ----------------------------------------------------------------------
# time helpers (local day/hour arithmetic, injectable for tests)
# ----------------------------------------------------------------------
def local_day(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None
                                                  else time.time()))


def day_start_ts(day):
    """Local-midnight timestamp of a YYYY-MM-DD day string."""
    return time.mktime(time.strptime(day, "%Y-%m-%d"))


def next_daily_run(now, hour):
    """Timestamp of the next occurrence of local `hour` (0-23) after now.
    Now-agnostic: at 08:59 with hour=9 -> today 09:00; at 09:01 -> tomorrow."""
    hour = max(0, min(23, int(hour)))
    target = day_start_ts(local_day(now)) + hour * 3600
    if target <= now:
        target += 86400
    return target


# ----------------------------------------------------------------------
# journal
# ----------------------------------------------------------------------
class DigestJournal:
    """Append-only event list, JSON-file backed, pruned by day."""

    def __init__(self, state_dir):
        self.dir = state_dir
        self.path = os.path.join(state_dir, JOURNAL_FILE)
        self.events = self._load()

    # -- persistence ----------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)
                        and e.get("kind")]
        except (OSError, ValueError):
            pass
        return []

    def _save(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            cut = local_day(time.time() - cfg.DIGEST_KEEP_DAYS * 86400)
            self.events = [e for e in self.events
                           if str(e.get("day", "9999")) >= cut]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.events, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass                      # the digest is a convenience; the
                                          # receiver must never break on it

    # -- recording ------------------------------------------------------
    def record(self, kind, t=None, **fields):
        ev = {"t": round(t if t is not None else time.time(), 1),
              "day": local_day(t if t is not None else None),
              "kind": kind}
        ev.update(fields)
        self.events.append(ev)
        self._save()
        return ev

    def for_day(self, day):
        return [e for e in self.events if e.get("day") == day]

    def days(self):
        return sorted({e.get("day") for e in self.events
                       if e.get("day")}, reverse=True)

    def prune(self):
        self._save()


# ----------------------------------------------------------------------
# digest text (pure formatting -> unit-testable)
# ----------------------------------------------------------------------
def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _bands_txt(bands):
    return "+".join(bands or []) or "all"


def _fmt_freq(band, hz):
    if band == "am":
        return f"{hz / 1e3:.0f} kHz"
    return f"{hz / 1e6:.2f} MHz"


def _station_tag(s):
    return f"{s.get('name') or '?'} {_fmt_freq(s.get('band'), s.get('freq_hz', 0))}"


def format_digest(day, events):
    """Compile one local day of journal events into the digest text."""
    evs = sorted([e for e in (events or []) if e.get("day") == day],
                 key=lambda e: e.get("t", 0))
    sweeps = [e for e in evs if e["kind"] == "sweep"]
    banks = [e for e in evs if e["kind"] == "stations"]
    pis = [e for e in evs if e["kind"] == "pi_lookup"]
    ais = [e for e in evs if e["kind"] == "ai_named"]
    audits = [e for e in evs if e["kind"] == "audit"]
    notes = [e for e in evs if e["kind"] == "scheduled_notes"]

    lines = [f"RTL-SDR Player — daily digest — {day}", "=" * 52]
    if not evs:
        lines.append("The receiver recorded no activity on this day "
                     "(no sweeps, no preset changes).")
        return "\n".join(lines)

    # -- sweeps ----------------------------------------------------------
    if sweeps:
        sched = sum(1 for e in sweeps if e.get("trigger") == "scheduled")
        total = sum(int(e.get("found") or 0) for e in sweeps)
        trig = f"{len(sweeps)} ({sched} scheduled, {len(sweeps) - sched} manual)"
        lines.append(f"Sweeps: {trig} · {total} station detections")
        for e in sweeps:
            row = (f"  {_hhmm(e['t'])} {e.get('trigger') or 'manual'} sweep "
                   f"{_bands_txt(e.get('bands'))} -> "
                   f"{e.get('found') or 0} stations")
            if e.get("am_floor_db") is not None:
                row += f" · AM floor {e['am_floor_db']:.0f} dB"
            if e.get("seconds"):
                row += f" · {e['seconds']:.0f} s"
            lines.append(row)

    # -- preset bank ------------------------------------------------------
    if banks:
        added = sum(len(e.get("added") or []) for e in banks)
        removed = sum(len(e.get("removed") or []) for e in banks)
        renamed = sum(len(e.get("renamed") or []) for e in banks)
        lines.append(f"Preset bank: {added} added · {removed} removed · "
                     f"{renamed} renamed across {len(banks)} updates")
        for e in banks:
            bits = []
            for s in (e.get("added") or [])[:6]:
                bits.append("+ " + _station_tag(s))
            for s in (e.get("removed") or [])[:6]:
                bits.append("- " + _station_tag(s))
            for r in (e.get("renamed") or [])[:6]:
                bits.append(f"~ {r.get('from') or '?'} -> "
                            f"{r.get('to') or '?'}")
            more = (len(e.get("added") or []) + len(e.get("removed") or [])
                    + len(e.get("renamed") or []))
            head = (f"  {_hhmm(e['t'])} [{e.get('source') or 'edit'}]"
                    + (f" ({more} changes)" if len(bits) < more else ""))
            lines.append(head)
            for b in bits:
                lines.append("    " + b)

    # -- online PI renames -------------------------------------------------
    if pis:
        named = sum(int(e.get("named") or 0) for e in pis)
        lines.append(f"PI lookups: {named} station(s) identified online")
    if ais:
        upd = sum(int(e.get("updated") or 0) for e in ais)
        if upd:
            lines.append(f"AI naming: {upd} preset(s) given AI names")

    # -- RDS audits --------------------------------------------------------
    if audits:
        ch = sum(int(e.get("channels") or 0) for e in audits)
        scores = [e.get("avg_score") for e in audits
                  if isinstance(e.get("avg_score"), (int, float))]
        avg = f" · avg score {sum(scores) / len(scores):.0f}" if scores else ""
        lines.append(f"RDS audits: {len(audits)} · {ch} channel checks{avg}")
        for e in audits:
            row = f"  {_hhmm(e['t'])} audit {e.get('channels') or 0} ch"
            if isinstance(e.get("avg_score"), (int, float)):
                row += f" · avg {e['avg_score']:.0f}"
            if e.get("worst"):
                row += f" · weakest: {e['worst']}"
            lines.append(row)

    for e in notes:
        if e.get("notes"):
            lines.append(f"  {_hhmm(e['t'])} scheduler: " +
                         "; ".join(e["notes"]))

    if len(lines) == 2:
        lines.append("No reportable changes.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# built digests on disk
# ----------------------------------------------------------------------
class DigestStore:
    """The built digests: <state>/digests/YYYY-MM-DD.txt (+ .json meta)."""

    def __init__(self, state_dir):
        self.dir = os.path.join(state_dir, DIGESTS_SUBDIR)

    def save(self, day, text):
        try:
            os.makedirs(self.dir, exist_ok=True)
            p = os.path.join(self.dir, f"{day}.txt")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, p)
            return p
        except OSError:
            return None

    def get(self, day):
        p = os.path.join(self.dir, f"{day}.txt")
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def exists(self, day):
        return os.path.exists(os.path.join(self.dir, f"{day}.txt"))

    def days(self):
        try:
            return sorted((f[:-4] for f in os.listdir(self.dir)
                           if f.endswith(".txt")), reverse=True)
        except OSError:
            return []


# ----------------------------------------------------------------------
# email (stdlib, best-effort)
# ----------------------------------------------------------------------
def email_configured(a):
    """True when the agent config carries a usable SMTP target."""
    return bool(a.get("digest_smtp_host") and a.get("digest_email_to"))


def send_email(a, subject, body, timeout=20):
    """Send a plain-text email via SMTP. Returns (ok, note); never raises.
    Uses STARTTLS when the port is 587, implicit SSL on 465."""
    if not a.get("digest_email_enabled"):
        return False, "email disabled"
    if not email_configured(a):
        return False, "email target incomplete (host / recipient missing)"
    host = str(a.get("digest_smtp_host")).strip()
    port = int(a.get("digest_smtp_port") or 587)
    user = str(a.get("digest_smtp_user") or "")
    pwd = str(a.get("digest_smtp_pass") or "")
    frm = str(a.get("digest_email_from") or user or "rtl-sdr-player@localhost")
    to = str(a.get("digest_email_to")).strip()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    try:
        socket.setdefaulttimeout(timeout)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as s:
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                try:
                    s.starttls()
                except smtplib.SMTPNotSupportedError:
                    pass               # plaintext relay (e.g. local MTA)
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        return True, f"email sent to {to}"
    except Exception as exc:               # SMTP auth/TLS/IO/DNS — all data
        return False, f"email failed: {exc}"
    finally:
        socket.setdefaulttimeout(None)
