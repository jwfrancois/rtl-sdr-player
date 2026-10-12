"""Agent layer (v1.5): LLM station naming, dial reports, natural-language
control, and the brains behind the scheduled background re-sweep.

Everything here degrades gracefully. With no LLM configured the app still
gets: rule-based voice-style commands, a template dial report, and the
full scheduled-sweep engine. When an OpenAI-compatible endpoint is
configured (OpenAI, Z.ai GLM, Ollama / LM Studio on localhost, vLLM...),
the agent additionally proposes friendly station names and understands
fuzzier commands.

Only the Python standard library is used for the HTTP call, so no new
pip dependencies are introduced.
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import pi_db as pi_db_mod

# Persisted to agent_config.json by the engine (stream.py).
AGENT_DEFAULTS = {
    # background re-sweep schedule
    "sweep_interval_min": 0,       # 0 = off; otherwise >= 5 minutes
    "sweep_bands": None,           # None = all bands (wfm, am, wx, air)
    "sweep_auto_fill": True,       # refill the 20 presets after each sweep
    "sweep_allow_listeners": False,  # sweep even while a browser is open
    # LLM naming / fuzzy intent (OpenAI-compatible chat completions)
    "llm_enabled": False,
    "llm_base_url": "",            # e.g. https://api.openai.com/v1
    "llm_model": "",               # e.g. gpt-4o-mini
    "llm_api_key": "",             # stays on this machine
    "auto_enrich": True,           # AI-name presets after scheduled sweeps
    # v1.6: AM sweep sensitivity + PI-code online callsign lookup
    "am_sensitivity": "medium",    # low | medium | high (noise-floor margin)
    "pi_lookup_enabled": True,     # resolve PI codes via online databases
    "pi_lookup_url": "",           # optional custom URL template with {pi}
}


class AgentError(Exception):
    """Raised for LLM/network failures; always converted to a friendly
    reply upstream, never allowed to take the app down."""


# ----------------------------------------------------------------------
# PI-code online callsign lookup (v1.6; cache-fronted, never raises)
# ----------------------------------------------------------------------
def pi_lookup(pi, acfg, force=False):
    """Resolve a PI code to a station name via the online databases.
    See backend/pi_db.py for the provider chain; failures are data
    ({"ok": False, ...}), never exceptions."""
    try:
        return pi_db_mod.lookup_pi(pi, acfg, force=force)
    except Exception as exc:      # belt & braces on top of the module guard
        return {"ok": False, "pi": pi, "name": None, "source": None,
                "cached": False, "error": str(exc)[:80]}


# ----------------------------------------------------------------------
# US callsign hints from RDS PI codes (NRSC-4 encoding, conservative)
# ----------------------------------------------------------------------
def _pi_idx_to_letters(idx):
    idx = int(idx)
    return chr(65 + idx // 676) + chr(65 + (idx // 26) % 26) + chr(65 + idx % 26)


def pi_callsign_candidates(pi):
    """Possible US callsigns for a PI code, or [] when ambiguous/none.

    K-call PI = 21672 + idx, W-call PI = 21672 + 4096 + idx, where idx
    encodes the last three letters base-26. The K and W arithmetic ranges
    overlap, so a PI in the overlap band is reported as ambiguous (no
    guess) instead of risking a wrong name on a preset.
    """
    try:
        if isinstance(pi, str):
            pi = int(pi, 16)
        pi = int(pi)
    except (TypeError, ValueError):
        return []
    out = []
    if 21672 <= pi <= 25767:            # K-only region
        out.append("K" + _pi_idx_to_letters(pi - 21672))
    elif 39248 <= pi <= 43343:          # W-only region
        out.append("W" + _pi_idx_to_letters(pi - 25768))
    return out


# ----------------------------------------------------------------------
# OpenAI-compatible chat completion (stdlib urllib)
# ----------------------------------------------------------------------
def llm_chat(messages, acfg, timeout=30, max_tokens=700):
    """One chat completion round trip. Raises AgentError on any failure."""
    base = str(acfg.get("llm_base_url") or "").strip().rstrip("/")
    key = str(acfg.get("llm_api_key") or "").strip()
    model = str(acfg.get("llm_model") or "").strip()
    if not base or not model:
        raise AgentError("LLM base URL / model not configured")
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={
                                     "Content-Type": "application/json",
                                     "Authorization": "Bearer " + key,
                                 })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError) as exc:
        raise AgentError(f"LLM request failed: {exc}") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AgentError(f"unexpected LLM reply shape: {data}") from exc
    if not content:
        raise AgentError("empty LLM reply")
    return content


def _extract_json(text):
    """Best-effort JSON extraction from an LLM answer (fences, prose)."""
    t = text.strip()
    if "```" in t:                       # prefer fenced blocks if present
        m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
        if m:
            t = m.group(1).strip()
    lo, hi = t.find("{"), t.rfind("}")
    if lo >= 0 and hi > lo:
        try:
            return json.loads(t[lo:hi + 1])
        except ValueError:
            pass
    lo, hi = t.find("["), t.rfind("]")
    if lo >= 0 and hi > lo:
        try:
            return json.loads(t[lo:hi + 1])
        except ValueError:
            pass
    raise AgentError("no JSON found in LLM reply")


# ----------------------------------------------------------------------
# LLM station naming
# ----------------------------------------------------------------------
NAMING_SYSTEM = (
    "You are the naming assistant of an RTL-SDR radio receiver. You get "
    "the receiver's station presets as JSON. Return STRICT JSON only, no "
    "prose: {\"stations\":[{\"slot\":1,\"name\":\"...\",\"genre\":\"...\"}]} "
    "for every slot given. Rules: names at most 18 characters, ALL-CAPS "
    "like real radio displays. If a callsign candidate is given, prefer it. "
    "Keep an existing RDS name when the name already looks like an RDS PS "
    "(short, no 'FM 96.1' style). Otherwise invent the most plausible "
    "display name from band + frequency + signal: US FM -> callsign style; "
    "AM 530-1700 kHz -> news/talk heritage style but NEVER invent a real "
    "brand you are not sure about, fall back to 'AM <freq>'; "
    "NOAA weather -> 'NOAA WX<n>'; airband 118-137 -> role by frequency "
    "(GUARD 121.5, CTAF/UNICOM near 122.8-123.0, TOWER/APP/DEP elsewhere). "
    "genre: 1-3 words like News/Talk, Classic Rock, Weather, ATC, Jazz."
)


def enrich_stations(stations, acfg):
    """Ask the LLM for friendly names/genres. Pure function: takes the
    station list, returns [{slot, name, genre}] (only slots that got a
    usable answer). Raises AgentError on transport/parse failures."""
    rows = []
    for st in stations or []:
        if not st:
            continue
        row = {"slot": st.get("slot"), "band": st.get("band"),
               "freq_mhz": round(st.get("freq_hz", 0) / 1e6, 4),
               "name": st.get("name", "")}
        if st.get("mode"):
            row["mode"] = st["mode"]
        cands = pi_callsign_candidates(st.get("pi"))
        if cands:
            row["callsign_candidate"] = cands[0]
        rows.append(row)
    if not rows:
        return []
    content = llm_chat(
        [{"role": "system", "content": NAMING_SYSTEM},
         {"role": "user", "content": json.dumps({"stations": rows})}],
        acfg, timeout=45, max_tokens=900)
    data = _extract_json(content)
    items = data.get("stations") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AgentError("LLM naming reply has no stations[] list")
    out = []
    for it in items:
        try:
            slot = int(it.get("slot"))
            name = str(it.get("name") or "").strip()[:18]
        except (AttributeError, TypeError, ValueError):
            continue
        if not name:
            continue
        out.append({"slot": slot, "name": name,
                    "genre": str(it.get("genre") or "").strip()[:24]})
    return out


# ----------------------------------------------------------------------
# Dial report ("what's on air?")
# ----------------------------------------------------------------------
def _fmt_freq(band, hz):
    if band == "am":
        return f"{hz / 1e3:.0f} kHz"
    return f"{hz / 1e6:.2f} MHz"


def _report_data(engine):
    st = engine.status()
    stations = [s for s in (engine.stations_list() or []) if s]
    results = engine.sweep_results_list() or []
    rds = engine.current_rds() or {}
    meta = getattr(engine, "sweep_meta", None) or {}
    named = sum(1 for s in stations
                if s.get("ai_name") or not re.match(
                    r"^(FM|AM|NOAA|AIR) [\d.]", s.get("name", "")))
    return {
        "tuned": {"band": st["band"], "freq_hz": st["freq_hz"],
                  "mode": st.get("demod_mode"),
                  "rds_ps": rds.get("ps"), "rds_pi": rds.get("pi"),
                  "chan_db": st.get("chan_db"),
                  "device": st.get("mode")},
        "last_sweep": {"t": meta.get("t"),
                       "bands": meta.get("bands"),
                       "count": len(results)},
        "top": [{"band": r["band"], "freq_hz": r["freq_hz"],
                 "name": r.get("name"), "db": r.get("db")}
                for r in results[:10]],
        "presets": {"filled": len(stations), "named": named},
    }


def _template_report(data):
    d = data
    tuned = d["tuned"]
    line = f"{tuned['band'].upper()} {_fmt_freq(tuned['band'], tuned['freq_hz'])}"
    if tuned.get("rds_ps"):
        line += f' "{tuned["rds_ps"]}"'
    if tuned.get("chan_db") is not None:
        line += f" · channel {tuned['chan_db']:.1f} dB"
    line += f" · {tuned.get('device') or '?'} device"
    lines = ["DIAL REPORT", "Tuned: " + line]
    sw = d["last_sweep"]
    if sw.get("t"):
        tstr = time.strftime("%H:%M", time.localtime(sw["t"]))
        bands = "+".join((sw.get("bands") or [])) or "all"
        lines.append(f"Last sweep: {tstr} · {bands} · "
                     f"{sw.get('count', 0)} stations found")
    if d["top"]:
        tops = ", ".join(
            f'{r.get("name") or "?"} {(_fmt_freq(r["band"], r["freq_hz"]))}'
            for r in d["top"][:5])
        lines.append(f"Strongest: {tops}")
    p = d["presets"]
    lines.append(f"Presets: {p['filled']}/20 filled · {p['named']} named")
    tips = []
    if p["filled"] < 20:
        tips.append('run "sweep all" then fill presets for more coverage')
    if not d["last_sweep"].get("t"):
        tips.append('no sweep yet — say "sweep all" to map the dial')
    lines.append("Tips: " + ("; ".join(tips) if tips else
                            'ask me things like "tune 101.7", '
                            '"scan airband", "next station"'))
    return "\n".join(lines)


def dial_report(engine, acfg):
    """What's on air? Template answer always works; a configured LLM adds
    a short narrative on top of the same measured data."""
    data = _report_data(engine)
    text = _template_report(data)
    ai = False
    if (acfg.get("llm_enabled") and acfg.get("llm_base_url")
            and acfg.get("llm_api_key")):
        try:
            content = llm_chat(
                [{"role": "system",
                  "content": "You are the receiver agent of an RTL-SDR "
                             "web radio. Summarize the measured dial data "
                             "in 3-5 friendly sentences for the user. "
                             "Mention concrete frequencies and names. No "
                             "invented stations, no markdown headers."},
                 {"role": "user", "content": json.dumps(data)}],
                acfg, timeout=40, max_tokens=350)
            text = content.strip() + "\n\n--- data ---\n" + text
            ai = True
        except AgentError:
            pass                      # template fallback already in place
    return {"ok": True, "text": text, "ai": ai, "data": data}


# ----------------------------------------------------------------------
# Natural-language command interface (rule-based, LLM fallback)
# ----------------------------------------------------------------------
HELP = (
    "I can: tune 101.7 | am 1010 | 121.5 | 162.550 · "
    "scan fm / am / noaa / airband · sweep all (or: sweep fm wx) · "
    "stop scan · next / previous station · report (what's on?) · "
    "lookup pi 2A5B (online callsign lookup) · "
    "name the stations (AI) · sweep every 30 minutes / sweeps off · "
    "status. With an LLM configured I also understand fuzzier requests."
)

_BAND_WORDS = {
    "am": "am", "fm": "wfm", "wfm": "wfm",
    "air": "air", "airband": "air", "atc": "air", "aviation": "air",
    "wx": "wx", "weather": "wx", "noaa": "wx",
}
_SCAN_LIST_FOR = {"wfm": "fm_band", "am": "am_band", "wx": "noaa",
                  "air": "air_band"}


def _parse_freq_request(low):
    """Returns (band, freq_hz) inferred from text, or (None, None)."""
    band_word = None
    for w in ("airband", "weather", "noaa", "fm", "am", "air", "wx"):
        if re.search(r"\b" + w + r"\b", low):
            band_word = _BAND_WORDS.get(w)
            break
    m = re.search(r"(\d{1,4}(?:\.\d{1,3})?)\s*(mhz|khz|k)?\b", low)
    if not m:
        return None, None
    val = float(m.group(1))
    unit = m.group(2)
    if band_word == "am":
        hz = val * (1e3 if unit in (None, "khz", "k") and val >= 500
                    else (1e6 if val < 30 else 1e3))
        return "am", int(hz)
    if unit == "khz" or unit == "k":
        val = val / 1e3                      # -> MHz
    if 530 <= val <= 1700 and (val == int(val) or val > 100):
        return "am", int(val * 1e3)
    if 87.5 <= val <= 108.0:
        return "wfm", int(val * 1e6)
    if 118.0 <= val <= 136.975:
        return "air", int(val * 1e6)
    if 162.3875 <= val <= 162.5625:
        return "wx", int(val * 1e6)
    if band_word == "wfm" and 76 <= val <= 108:
        return "wfm", int(val * 1e6)
    return None, None


def _stations_cycle(engine, step):
    stations = [s for s in (engine.stations_list() or []) if s]
    if not stations:
        return None, "No presets saved yet — run a sweep and fill them."
    cur = next((i for i, s in enumerate(stations)
                if s["band"] == engine.band
                and abs(s["freq_hz"] - engine.freq) < 500), None)
    if cur is None:
        idx = 0 if step > 0 else len(stations) - 1
    else:
        idx = (cur + step) % len(stations)
    st = stations[idx]
    engine.tune(band=st["band"], freq_hz=st["freq_hz"], mode=st.get("mode"))
    return st, None


def _apply_schedule(engine, minutes, acfg):
    """minutes: 0 (off) or a positive number of minutes."""
    engine.set_agent_settings({"sweep_interval_min": minutes})
    if minutes <= 0:
        return "Background re-sweep disabled."
    return (f"Scheduled: re-sweep every {minutes:g} min"
            + (" with auto-fill of the presets"
               if engine.agent.get("sweep_auto_fill") else "")
            + ".")


def _dispatch(action, args, engine, acfg):
    if action == "tune":
        applied = engine.tune(band=args.get("band"),
                              freq_hz=args.get("freq_hz"))
        band = applied.get("band") or engine.band
        hz = applied.get("freq_hz") or engine.freq
        freq = f"{hz / 1e3:.0f} kHz" if band == "am" else f"{hz / 1e6:.2f} MHz"
        rds = engine.current_rds() or {}
        # the RDS name belongs to the previous channel until the hop lands,
        # so only mention it when it was already tuned to this frequency
        extra = (f' — {rds["ps"]}' if rds.get("ps")
                 and band == engine.band and abs(engine.freq - hz) < 500
                 else "")
        return f"Tuned: {band.upper()} {freq}{extra}"
    if action == "scan":
        engine.start_scan(args.get("list"))
        label = args.get("list") or "list"
        return f"Scanning {label} — hold on activity, say 'stop' to end."
    if action == "sweep":
        res = engine.start_sweep(bands=args.get("bands"))
        return (f"Sweeping {res['channels']} channels across "
                f"{'+'.join(res['bands']).upper()} — audio mutes until "
                "done, presets refresh after.")
    if action == "stop":
        engine.stop_scan()
        return "Scan/sweep stopped."
    if action == "next":
        st, err = _stations_cycle(engine, +1)
        return err or f"Preset {st['slot']}: {st['name']}"
    if action == "prev":
        st, err = _stations_cycle(engine, -1)
        return err or f"Preset {st['slot']}: {st['name']}"
    if action == "report":
        return dial_report(engine, acfg)["text"]
    if action == "enrich":
        res = engine.enrich_station_names()
        if not res.get("ok"):
            return res.get("error", "AI naming unavailable.")
        return (f"AI named {res['updated']} presets"
                + ("." if res["updated"] else
                   " — nothing needed a name."))
    if action == "lookup":
        res = pi_lookup(args.get("pi"), acfg)
        if res.get("ok"):
            return (f'PI {res["pi"]}: {res["name"]}'
                    + (f' ({res["source"]})' if res.get("source") else ""))
        return (f'PI {args.get("pi") or "?"}: '
                f'{res.get("error") or "no match (offline)"}')
    if action == "schedule":
        return _apply_schedule(engine, args.get("minutes", 0), acfg)
    if action == "status":
        st = engine.status()
        f = st["freq_hz"]
        freq = f"{f / 1e3:.0f} kHz" if st["band"] == "am" \
            else f"{f / 1e6:.3f} MHz"
        rds = engine.current_rds() or {}
        return (f"You are on {st['band'].upper()} {freq}"
                + (f' — "{rds["ps"]}"' if rds.get("ps") else "")
                + f" · device {st.get('mode')}"
                + f" · {sum(1 for s in st['stations'] if s)}/20 presets "
                "filled.")
    return None


def execute_command(engine, text, acfg):
    """Parse and run a user command. Always returns a reply dict; user
    input mistakes (bad freq, short interval) come back as replies,
    never as HTTP 500s."""
    try:
        return _execute_command(engine, text, acfg)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "action": "error", "reply": str(exc)}


def _execute_command(engine, text, acfg):
    raw = (text or "").strip()
    low = raw.lower()

    def done(action, reply, **extra):
        return {"ok": True, "action": action, "reply": reply, **extra}

    if not raw:
        return {"ok": False, "action": "help", "reply": HELP}
    if low in ("help", "?", "commands", "what can you do"):
        return done("help", HELP)

    # stop
    if re.search(r"\bstop\b|\babort\b|\bcancel\b", low) and \
            not re.search(r"\bschedule\b", low):
        return done("stop", _dispatch("stop", {}, engine, acfg))

    # schedule: "every 30 minutes", "sweeps off", "resweep hourly"
    m = re.search(r"\bevery\s+(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hours?)\b",
                  low)
    if m:
        val = float(m.group(1))
        if m.group(2).startswith("h"):
            val *= 60
        return done("schedule", _dispatch("schedule",
                                          {"minutes": val}, engine, acfg))
    if re.search(r"\bsweep[s]?\b.*\b(off|disable|stop)\b|\b(off|disable)\b\s+sweep", low):
        return done("schedule", _dispatch("schedule",
                                          {"minutes": 0}, engine, acfg))
    m = re.search(r"\bsweep[s]?\s+every\s+(hour|half.hour)\b", low)
    if m:
        return done("schedule", _dispatch(
            "schedule", {"minutes": 60 if "half" not in m.group(1) else 30},
            engine, acfg))

    # sweep now
    m = re.search(r"\b(sweep|rescan|re-?scan|full scan)\b(.*?$)", low)
    if m and not re.search(r"\bevery\b|\boff\b|\bstop\b", m.group(2)):
        tail = m.group(2)
        bands = [v for k, v in _BAND_WORDS.items()
                 if re.search(r"\b" + k + r"\b", tail)]
        if not bands and re.search(r"\b(all|everything|full|bands)\b|^\s*$", tail):
            bands = None
        elif not bands:
            bands = None
        else:
            bands = sorted(set(bands))
        return done("sweep", _dispatch("sweep", {"bands": bands},
                                       engine, acfg))

    # PI code online lookup: "lookup pi 2A5B", "pi F301", "identify BEEF"
    m = re.search(r"\b([0-9a-f]{4})\b", low)
    if m and re.search(r"\b(pi|lookup|identify|callsign)\b", low):
        pi = m.group(1).upper()
        reply = _dispatch("lookup", {"pi": pi}, engine, acfg)
        return done("lookup", reply, pi=pi)

    # scan a band list
    m = re.search(r"\bscan\b\s*(fm|am|wx|noaa|weather|airband|air)?", low)
    if m:
        word = m.group(1) or "fm"
        band = _BAND_WORDS.get(word, "wfm")
        lst = _SCAN_LIST_FOR.get(band, "fm_band")
        return done("scan", _dispatch("scan", {"list": lst}, engine, acfg))

    # AI naming
    if re.search(r"\b(ai|llm|smart)\b.*\bname|\bname\b.*\b(stations?|presets?|slots?)\b|\benrich\b|\bai\s*names?", low):
        return done("enrich", _dispatch("enrich", {}, engine, acfg))

    # report
    if re.search(r"\breport\b|what.?s on|what.?s playing|hear|summar(y|ize)|dial report", low):
        return done("report", _dispatch("report", {}, engine, acfg))

    # next / previous preset
    if re.search(r"\bnext\b|\bhop\b|\bskip\b", low):
        return done("next", _dispatch("next", {}, engine, acfg))
    if re.search(r"\bprevious\b|\bback\b|\bprev\b", low):
        return done("prev", _dispatch("prev", {}, engine, acfg))

    # status
    if re.search(r"\bstatus\b|what am i|what is this|which station|where am i", low):
        return done("status", _dispatch("status", {}, engine, acfg))

    # tune patterns (kept late so "what's on 101.7?" still reads as report-ish)
    band, hz = _parse_freq_request(low)
    if hz is not None:
        return done("tune", _dispatch("tune", {"band": band, "freq_hz": hz},
                                      engine, acfg))

    # LLM fallback for fuzzy requests
    if (acfg.get("llm_enabled") and acfg.get("llm_base_url")
            and acfg.get("llm_api_key")):
        st = engine.status()
        ctx = {"band": st["band"], "freq_hz": st["freq_hz"],
               "mode": st.get("demod_mode")}
        try:
            content = llm_chat(
                [{"role": "system",
                  "content": "You turn radio-player commands into STRICT "
                             "JSON actions. Allowed actions: "
                             '{"action":"tune","args":{"band":"wfm|am|wx|air","freq_hz":number}} | '
                             '{"action":"scan","args":{"list":"fm_band|am_band|noaa|air_band"}} | '
                             '{"action":"sweep","args":{"bands":["wfm","am","wx","air"] or null}} | '
                             '{"action":"stop","args":{}} | '
                             '{"action":"next","args":{}} | {"action":"prev","args":{}} | '
                             '{"action":"report","args":{}} | '
                             '{"action":"enrich","args":{}} | '
                             '{"action":"lookup","args":{"pi":"4 hex digits"}} | '
                             '{"action":"schedule","args":{"minutes":number (0=off)}} | '
                             '{"action":"status","args":{}} | '
                             "Pick the single closest action. JSON only."},
                 {"role": "user",
                  "content": f"current state: {json.dumps(ctx)}\n"
                             f"command: {raw}"}],
                acfg, timeout=30, max_tokens=120)
            intent = _extract_json(content)
            action = str(intent.get("action") or "").lower()
            args = intent.get("args") or {}
            if action in ("tune",):
                args.setdefault("band", None)
                if not isinstance(args.get("freq_hz"), (int, float)):
                    raise AgentError("no freq_hz")
            if action == "lookup":
                if not re.fullmatch(r"[0-9A-Fa-f]{4}",
                                    str(args.get("pi") or "")):
                    raise AgentError("no 4-digit hex pi")
            reply = _dispatch(action, args, engine, acfg)
            if reply is not None:
                return done(action + " (ai)", reply)
        except AgentError as exc:
            return {"ok": False, "action": "error",
                    "reply": f"AI helper failed: {exc}\n\n" + HELP}

    return {"ok": False, "action": "unknown",
            "reply": "I didn't catch that.\n" + HELP}
