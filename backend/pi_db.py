"""PI-code -> station name lookup (v1.6).

RDS gives us the station's PI code for free, but the PS name is often
missing (weak signal, short dwell) or useless ("RTDSD2832"). This module
resolves a PI code to a real station identity by asking online databases,
with a local cache in front so each PI is queried at most once per TTL
window and repeated sweeps stay instant.

Provider chain (first hit wins, all best-effort, never raises):
  1. local cache  (pi_cache.json next to app.py, positive 30 d / negative 6 h)
  2. user-configured endpoint (agent setting pi_lookup_url, a URL template
     containing {pi}; JSON replies with name/station/callsign keys and HTML
     pages are both understood)
  3. fmscan.org   (https://www.fmscan.org/pi.php?pi={pi}, HTML)
  4. fmdx.pl      (https://fmdx.pl/pi/?pi={pi}, HTML)

Only the Python standard library is used, so no new pip dependencies.
Everything degrades gracefully offline: lookup_pi() returns
{"ok": False, "error": ...} and the caller keeps the previous name.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

POS_TTL_S = 30 * 24 * 3600.0     # a station identity rarely changes
NEG_TTL_S = 6 * 3600.0           # retry misses after 6 hours
HTTP_TIMEOUT = 7.0

_PROVIDERS = ("custom", "fmscan", "fmdx")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_DEFAULT_FMSCAN = "https://www.fmscan.org/pi.php?pi={pi}"
_DEFAULT_FMDX = "https://fmdx.pl/pi/?pi={pi}"

# words that appear on lookup pages but are never a station name
_BAD_WORDS = re.compile(
    r"^(mhz|khz|pi|code|station|search|results?|home|fmscan|fmdx|error|"
    r"forbidden|welcome|login|menu|frequency|location|country|region|"
    r"format|power|distance|azimuth|polarization|it|tx|identity|date|"
    r"time|utc|detected|monitored|details?|name|notes?|comments?)$",
    re.I)

_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'\-/]{2,27}$")


class PiCache:
    """JSON-file backed {pi: {name, source, t}} cache with TTLs."""

    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # cache is a convenience; never break the caller

    def get(self, pi):
        ent = self.data.get(pi)
        if not ent:
            return None
        ttl = POS_TTL_S if ent.get("name") else NEG_TTL_S
        if time.time() - float(ent.get("t", 0)) > ttl:
            return None
        return ent

    def put(self, pi, name, source):
        self.data[pi] = {"name": name, "source": source,
                         "t": round(time.time(), 1)}
        # keep the file bounded (most-recently-set survive)
        if len(self.data) > 4000:
            keep = sorted(self.data.items(),
                          key=lambda kv: -kv[1].get("t", 0))[:3000]
            self.data = dict(keep)
        self.save()


def norm_pi(pi):
    """Normalize a PI code to 4 uppercase hex chars, or None."""
    s = str(pi or "").strip().upper()
    s = s[2:] if s.upper().startswith("0X") else s
    return s if re.fullmatch(r"[0-9A-F]{4}", s) else None


# ----------------------------------------------------------------------
# HTTP + page parsing
# ----------------------------------------------------------------------
def _fetch(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _text_nodes(html):
    """Visible text of an HTML page, one candidate per line."""
    html = re.sub(r"(?is)<(script|style|title)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    html = html.replace("&amp;", "&").replace("&#39;", "'")
    html = html.replace("&nbsp;", " ").replace("&quot;", '"')
    return [ln.strip() for ln in html.split("\n") if ln.strip()]


def _name_from_html(html):
    """Pull the most plausible station name out of a lookup page."""
    for ln in _text_nodes(html):
        if len(ln) < 3 or len(ln) > 28 or not _NAME_OK.match(ln):
            continue
        if _BAD_WORDS.match(ln):
            continue
        if re.search(r"(?i)\b(mhz|khz)\b", ln):     # frequency readings
            continue
        if re.fullmatch(r"[\d .:\-]+", ln):     # pure numbers/times
            continue
        if re.fullmatch(r"[0-9A-F]{4}", ln):    # the PI echo itself
            continue
        if "@" in ln or "http" in ln.lower():
            continue
        return ln[:28]
    return None


def _name_from_json(data):
    """Pick a station name out of a JSON reply (common key names)."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    for key in ("name", "station", "callsign", "call", "ps", "label",
                "station_name", "short_name"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:28]
    for key in ("results", "stations", "items", "data"):
        sub = data.get(key)
        if isinstance(sub, list) and sub:
            return _name_from_json(sub[0])
    return None


def _fetch_provider(url_tpl, pi):
    """One provider attempt -> (name, None) or (None, error)."""
    url = url_tpl.replace("{pi}", urllib.parse.quote(pi))
    try:
        body = _fetch(url)
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError) as exc:
        return None, str(exc)[:80]
    if not body:
        return None, "empty reply"
    # JSON endpoint?
    head = body.lstrip()[:1]
    if head in ("{", "["):
        try:
            return _name_from_json(json.loads(body)), None
        except ValueError:
            pass
    return _name_from_html(body), None


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
_default_cache = None          # module-level cache, set by init_cache()


def init_cache(path):
    """Point the module-level cache at a file (called by the engine)."""
    global _default_cache
    _default_cache = PiCache(path)
    return _default_cache


def lookup_pi(pi, acfg=None, cache=None, force=False):
    """Resolve a PI code to a station name. Never raises.

    Returns {"ok": bool, "pi": str, "name": str|None, "source": str|None,
             "cached": bool, "error": str|None}.
    acfg may carry pi_lookup_url (custom provider template); when it sets
    pi_lookup_enabled=False only the cache is consulted.
    """
    acfg = acfg or {}
    pi = norm_pi(pi)
    out = {"ok": False, "pi": pi, "name": None, "source": None,
           "cached": False, "error": None}
    if not pi:
        out["error"] = "PI must be 4 hex digits"
        return out

    cache = cache if cache is not None else _default_cache
    if cache is None:
        cache = init_cache(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pi_cache.json"))
    if not force:
        hit = cache.get(pi)
        if hit is not None:
            if hit.get("name"):
                out.update(ok=True, name=hit["name"],
                           source=hit.get("source"), cached=True)
            else:
                out["error"] = "no match (cached)"
            return out

    if acfg.get("pi_lookup_enabled") is False:
        out["error"] = "PI lookup disabled in Agent settings"
        return out

    urls = []
    custom = str(acfg.get("pi_lookup_url") or "").strip()
    if custom and "{pi}" in custom:
        urls.append(("custom", custom))
    urls.append(("fmscan", _DEFAULT_FMSCAN))
    urls.append(("fmdx", _DEFAULT_FMDX))

    last_err = None
    for source, tpl in urls:
        name, err = _fetch_provider(tpl, pi)
        last_err = err or last_err
        if name:
            cache.put(pi, name, source)
            out.update(ok=True, name=name, source=source)
            return out
    cache.put(pi, None, "miss")
    out["error"] = last_err or "no match online"
    return out
