"""v1.7 unit tests: scheduled-sweep change digest (backend.digest).

Covers the journal (record/prune/per-day queries), the pure digest text
formatter, the daily-schedule arithmetic, the digest store, and the SMTP
helper's graceful degradation. All file I/O happens in a temp dir; the
email tests never touch a real server.
"""
import os
import sys
import tempfile
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import digest as dg

ok = {}
fails = []


def check(name, cond, extra=""):
    ok[name] = bool(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="digest_test_")
    state = os.path.join(tmp, "state")

    # use today/yesterday: the journal prunes anything older than the
    # retention window (correct behavior - so test inside it)
    day0 = dg.local_day()                       # today
    day1 = dg.local_day(time.time() - 86400)    # yesterday
    day2 = dg.local_day(time.time() - 172800)   # two days ago

    # ------------------------------------------------ journal basics
    j = dg.DigestJournal(state)
    j.record("sweep", t=dg.day_start_ts(day1) + 3600,
             trigger="scheduled", bands=["wfm", "am"], found=12,
             am_floor_db=-48.2, seconds=123.4)
    j.record("stations", t=dg.day_start_ts(day1) + 3660,
             source="auto-fill",
             added=[{"slot": 1, "name": "SIM-RDS", "band": "wfm",
                     "freq_hz": 96_100_000}],
             removed=[], renamed=[])
    j.record("sweep", t=dg.day_start_ts(day0) + 7200,
             trigger="manual", bands=["wx"], found=2, seconds=9.0)

    check("journal day query", len(j.for_day(day1)) == 2)
    check("journal days sorted", j.days() == [day0, day1])

    # reload from disk (persistence + JSON round trip)
    j2 = dg.DigestJournal(state)
    check("journal persists", len(j2.for_day(day1)) == 2,
          len(j2.events))

    # ------------------------------------------------ digest text
    text10 = dg.format_digest(day1, j.events)
    print(text10)
    check("digest has header", day1 in text10)
    check("digest sweep line", "1 (1 scheduled, 0 manual)" in text10
          and "12 station detections" in text10)
    check("digest AM floor", "AM floor -48" in text10)
    check("digest stations line",
          "+ SIM-RDS 96.10 MHz" in text10 and "[auto-fill]" in text10)
    check("digest no-audit lines", "RDS audits" not in text10)

    text11 = dg.format_digest(day0, j.events)
    check("digest per-day isolation", "manual sweep wx" in text11
          and "SIM-RDS" not in text11)

    empty = dg.format_digest(day2, j.events)
    check("digest empty day", "recorded no activity" in empty)

    # renames + audits + PI/AI sections
    evs = [
        {"t": dg.day_start_ts(day2) + 100, "day": day2,
         "kind": "stations", "source": "PI lookup", "added": [],
         "removed": [],
         "renamed": [{"slot": 2, "from": "FM 98.5", "to": "KXYZ-FM"}]},
        {"t": dg.day_start_ts(day2) + 200, "day": day2,
         "kind": "audit", "channels": 5, "avg_score": 72.5,
         "worst": "FM 93.3", "seconds": 40.0},
        {"t": dg.day_start_ts(day2) + 300, "day": day2,
         "kind": "pi_lookup", "named": 3},
        {"t": dg.day_start_ts(day2) + 400, "day": day2,
         "kind": "scheduled_notes", "notes": ["auto-filled 5 presets"]},
    ]
    text13 = dg.format_digest(day2, evs)
    check("digest rename line", "FM 98.5 -> KXYZ-FM" in text13)
    check("digest audit line", "5 channel checks" in text13
          and "avg score 72" in text13 and "weakest: FM 93.3" in text13)
    check("digest pi line", "PI lookups: 3" in text13)
    check("digest notes line", "auto-filled 5 presets" in text13)

    # ------------------------------------------------ schedule arithmetic
    day_next = dg.local_day(time.time() + 86400)   # tomorrow
    noon = dg.day_start_ts(day0) + 12 * 3600
    nxt = dg.next_daily_run(noon, 9)
    check("next run skips past hour",
          time.strftime("%Y-%m-%d", time.localtime(nxt)) == day_next
          and time.strftime("%H", time.localtime(nxt)) == "09",
          f"{time.strftime('%F %H:%M', time.localtime(nxt))} want {day_next} 09:00")
    early = dg.day_start_ts(day0) + 2 * 3600      # 02:00
    nxt2 = dg.next_daily_run(early, 9)
    check("next run same day when hour ahead",
          time.strftime("%Y-%m-%d", time.localtime(nxt2)) == day0)
    nxt3 = dg.next_daily_run(noon, 24)                    # clamp hour
    check("hour clamps to 23",
          time.strftime("%H", time.localtime(nxt3)) == "23")

    # ------------------------------------------------ digest store
    store = dg.DigestStore(state)
    check("store save+get", store.save(day1, "HELLO DIGEST")
          and store.get(day1) == "HELLO DIGEST")
    check("store exists", store.exists(day1)
          and not store.exists(day2))
    store.save(day0, "OLD ONE")
    check("store days list", store.days() == [day0, day1])

    # ------------------------------------------------ email helpers
    a_off = {"digest_email_enabled": False, "digest_smtp_host": "x",
             "digest_email_to": "y"}
    sent, note = dg.send_email(a_off, "s", "b")
    check("email disabled is a no-op", sent is False and "disabled" in note)

    a_partial = {"digest_email_enabled": True, "digest_smtp_host": "",
                 "digest_email_to": ""}
    sent, note = dg.send_email(a_partial, "s", "b")
    check("email unconfigured degrades", sent is False
          and "incomplete" in note)

    a_bad = {"digest_email_enabled": True, "digest_smtp_host": "127.0.0.1",
             "digest_smtp_port": 1, "digest_smtp_user": "u",
             "digest_smtp_pass": "p", "digest_email_from": "f",
             "digest_email_to": "t@example.com"}
    sent, note = dg.send_email(a_bad, "s", "b", timeout=3)
    check("email failure is data not exception", sent is False
          and "failed" in note, note[:60])

    check("email_configured", dg.email_configured(a_bad) is True
          and dg.email_configured(a_partial) is False)

    print()
    if fails:
        print("DIGEST TEST FAILURES:", fails)
        return 1
    print(f"DIGEST TEST: ALL PASS ({len(ok)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
