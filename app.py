#!/usr/bin/env python3
"""
SM MAL SUB by Nandu10 — standalone service.

Malayalam subtitles from Msone + Movie Mirror + Team GOAT, one entry
per source. Subtitles only — video comes from your own sources.

Routes:
  /manifest.json
  /subtitles/<type>/<id>.json
  /srt/<src>/<key>.srt

Data (in ./data/):
  mm_data.json     Movie Mirror items (imdb_id -> sub_url)
  goat_data.json   Team GOAT items (imdb_id -> sub_url)
  mzone_data.json  Msone items (imdb_id -> post_url)
"""
import html
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import zipfile

from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

# Official Msone subtitle addon (bypasses Cloudflare via download-manager URLs)
MSONE_OFFICIAL_API = "https://addon.malayalamsubtitles.org"

# TMDB API for IMDb -> title lookup (live subtitle search).
# Set TMDB_API_KEY in Render env vars. Live-fetch is skipped if unset.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# Live-fetch endpoints
MM_WP_API = "https://moviemirrorsubtitles.com/wp-json/wp/v2"
GOAT_HOME = "https://malayalamsubtitles.in/"
LIVE_URL_TTL = 7 * 24 * 3600   # found live SRT URLs rarely change

# ---------------- subtitle data ----------------
_mm_data = None
_goat_data = None
_mzone_data = None
_mm_by_tt = None
_goat_by_tt = None
_mzone_by_tt = None


def _load_json(name):
    p = os.path.join(DATA_DIR, name)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def mm_items():
    global _mm_data
    if _mm_data is None:
        _mm_data = _load_json("mm_data.json").get("items", [])
    return _mm_data


def goat_items():
    global _goat_data
    if _goat_data is None:
        _goat_data = _load_json("goat_data.json").get("items", [])
    return _goat_data


def mzone_items():
    """All Msone items (sections + archive) with an imdb_id."""
    global _mzone_data
    if _mzone_data is None:
        d = _load_json("mzone_data.json")
        out = []
        seen = set()

        def collect(o):
            if isinstance(o, list):
                for it in o:
                    if isinstance(it, dict) and it.get("imdb_id"):
                        key = (it["imdb_id"], it.get("post_url"))
                        if key not in seen:
                            seen.add(key)
                            out.append(it)
            elif isinstance(o, dict):
                for v in o.values():
                    collect(v)

        collect(d.get("sections"))
        collect(d.get("archive"))
        _mzone_data = out
    return _mzone_data


def _index_by_tt(items):
    """imdb_id -> item or [items] (series can have several posts)."""
    idx = {}
    for it in items:
        tt = it.get("imdb_id")
        if not tt:
            continue
        if it.get("type") == "series" or it.get("media") == "tv":
            cur = idx.get(tt)
            if isinstance(cur, list):
                cur.append(it)
            elif isinstance(cur, dict):
                idx[tt] = [cur, it]
            else:
                idx[tt] = [it]
        else:
            idx.setdefault(tt, it)
    return idx


def mm_by_tt():
    global _mm_by_tt
    if _mm_by_tt is None:
        _mm_by_tt = _index_by_tt(mm_items())
    return _mm_by_tt


def goat_by_tt():
    global _goat_by_tt
    if _goat_by_tt is None:
        _goat_by_tt = _index_by_tt(goat_items())
    return _goat_by_tt


def mzone_by_tt():
    global _mzone_by_tt
    if _mzone_by_tt is None:
        _mzone_by_tt = _index_by_tt(mzone_items())
    return _mzone_by_tt


def _pick_series(items, season):
    """Pick the season post matching the requested season number."""
    if not isinstance(items, list):
        items = [items]
    if season is None:
        return items[0]
    s2 = "%02d" % season
    for it in items:
        blob = ((it.get("sub_url") or "") + " " + (it.get("post_url") or "")
                + " " + (it.get("name_en") or "") + " "
                + (it.get("name") or "")).lower()
        if (f"s{s2}" in blob or f"season {season}" in blob
                or f"season{s2}" in blob):
            return it
    return items[0]


# ---------------- subtitle fetch engine ----------------
SUB_CACHE_DIR = os.path.join(BASE_DIR, "sub_cache")
SUB_INDEX = os.path.join(SUB_CACHE_DIR, "index.json")
SUB_TTL = 7 * 24 * 3600   # subtitle bytes rarely change
MISS_TTL = 6 * 3600       # remember misses for 6h
SITE_GAP = 1.5            # politeness gap between site fetches

os.makedirs(SUB_CACHE_DIR, exist_ok=True)

_sub_lock = threading.Lock()
_sub_last_fetch = 0.0


def _sub_fetch(url, referer=None, timeout=30, retries=3):
    """Polite GET with browser UA via curl subprocess."""
    global _sub_last_fetch
    last_err = None
    for attempt in range(retries):
        with _sub_lock:
            wait = SITE_GAP - (time.time() - _sub_last_fetch)
            if wait > 0:
                time.sleep(wait)
            try:
                cmd = ["curl", "-sL", "--max-time", str(timeout),
                       "-A", UA, url]
                if referer:
                    cmd += ["-e", referer]
                r = subprocess.run(cmd, capture_output=True,
                                   timeout=timeout + 10)
                if r.returncode != 0 or not r.stdout:
                    raise IOError(f"curl rc={r.returncode}")
                return r.stdout
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
            finally:
                _sub_last_fetch = time.time()
    raise IOError(f"_sub_fetch failed after {retries}: {last_err}")


def _sub_index_load():
    try:
        with open(SUB_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sub_index_save(idx):
    tmp = SUB_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, SUB_INDEX)


def _to_utf8(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace").encode("utf-8")


def _pick_from_zip(data, season=None, episode=None):
    """Extract the right .srt from a zip. For series, match SXXEXX."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".srt", ".ass", ".vtt"))
                 and not n.startswith("__MACOSX")]
        if not names:
            return None
        if season is not None and episode is not None:
            pat = re.compile(r"s%02d\s*e%02d" % (season, episode), re.I)
            for n in names:
                if pat.search(n):
                    return z.read(n)
            pat2 = re.compile(r"%dx%02d" % (season, episode), re.I)
            for n in names:
                if pat2.search(n):
                    return z.read(n)
            return None  # specific episode requested but not in zip
        names.sort(key=lambda n: (0 if n.lower().endswith(".srt") else 1, n))
        return z.read(names[0])
    return None


def _sub_key(src, rid):
    safe = re.sub(r"[^A-Za-z0-9]+", "_", rid).strip("_")
    return f"{src}_{safe}"


# Msone download-link patterns (post page -> .srt/.zip file)
_MSONE_DL_PATTERNS = [
    re.compile(r'href="([^"]+?\.srt[^"]*)"', re.I),
    re.compile(r"href='([^']+?\.srt[^']*)'", re.I),
    re.compile(r'href="([^"]+?\.zip[^"]*)"', re.I),
    re.compile(r"href='([^']+?\.zip[^']*)'", re.I),
    re.compile(r'href="([^"]+?\.ass[^"]*)"', re.I),
]
_MSONE_DL_TEXT_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>([^<]*(?:download|'
    r'\u0d21\u0d57\u0d7a\u0d4d\u200d\u0d32\u0d4b\u0d21\u0d4d|'
    r'\u0d38\u0d2c\u0d4d\u0d1f\u0d48\u0d31\u0d4d\u0d31\u0d3f\u0d32\u0d4d)'
    r'[^<]*)</a>', re.I)


def _msone_download_url(post_html, post_url):
    for pat in _MSONE_DL_PATTERNS:
        m = pat.search(post_html)
        if m:
            return urllib.parse.urljoin(post_url, html.unescape(m.group(1)))
    m = _MSONE_DL_TEXT_RE.search(post_html)
    if m:
        return urllib.parse.urljoin(post_url, html.unescape(m.group(1)))
    return None


def _resolve_subtitle(src, rid, imdb_id, season=None, episode=None):
    """Download (or load from cache) the subtitle for one source.

    Returns srt bytes or None. src in ('mm', 'goat', 'msone').
    """
    key = _sub_key(src, rid)
    idx = _sub_index_load()
    now = time.time()
    entry = idx.get(key)
    if entry:
        if entry.get("miss") and now - entry["at"] < MISS_TTL:
            return None
        path = os.path.join(SUB_CACHE_DIR, key + ".srt")
        if (not entry.get("miss") and now - entry["at"] < SUB_TTL
                and os.path.exists(path)):
            with open(path, "rb") as f:
                return f.read()
    # live lookup
    item = None
    file_url = None
    referer = None
    try:
        if src == "mm":
            e = mm_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
            else:
                got = _mm_live_file_url(imdb_id, season)
                if got:
                    file_url, referer = got
        elif src == "goat":
            e = goat_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
            else:
                got = _goat_live_file_url(imdb_id, season)
                if got:
                    file_url, referer = got
        elif src == "msone":
            got = _msone_file_url(imdb_id, season)
            if got:
                file_url, referer = got
        if not file_url:
            raise ValueError("no subtitle file url")
        raw = _sub_fetch(file_url, referer=referer)
        if file_url.lower().split("?")[0].endswith(".zip") or raw[:2] == b"PK":
            srt = _pick_from_zip(raw, season, episode)
            if not srt:
                raise ValueError("no matching srt in zip")
        else:
            srt = raw
        srt = _to_utf8(srt)
    except Exception:
        idx[key] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    with open(os.path.join(SUB_CACHE_DIR, key + ".srt"), "wb") as f:
        f.write(srt)
    idx[key] = {"at": now}
    _sub_index_save(idx)
    return srt


def _msone_file_url(imdb_id, season=None):
    """Returns the direct subtitle file URL for an Msone title, or None.

    Msone post pages are behind Cloudflare which often blocks automated
    fetches — so we check live (with caching) instead of trusting the
    index alone. Returns None quickly on 403/block.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"msone_url:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < SUB_TTL:
            return e["url"], e.get("referer")
    entry = mzone_by_tt().get(imdb_id)
    item = _pick_series(entry, season) if entry else None
    if not item or not item.get("post_url"):
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    post_url = item["post_url"]
    try:
        page = _sub_fetch(post_url, timeout=12, retries=1)
        page_html = page.decode("utf-8", errors="replace")
        if "Just a moment" in page_html or "cf-challenge" in page_html:
            raise ValueError("cloudflare challenge")
        file_url = _msone_download_url(page_html, post_url)
        if not file_url:
            raise ValueError("no download link")
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": post_url}
    _sub_index_save(idx)
    return file_url, post_url


def _api_fetch_json(url, timeout=12):
    """Quick JSON GET (for TMDB / WP APIs). No politeness gap."""
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0 or not r.stdout:
        raise IOError("api fetch failed")
    return json.loads(r.stdout.decode("utf-8", errors="replace"))


def _norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tmdb_title(imdb_id):
    """Returns (title, year, media_type) for an IMDb ID via TMDB.

    Cached for 7 days. Returns (None, None, None) if no API key or
    lookup fails.
    """
    if not TMDB_API_KEY:
        return None, None, None
    idx = _sub_index_load()
    now = time.time()
    ck = f"tmdb:{imdb_id}"
    e = idx.get(ck)
    if e and not e.get("miss") and now - e["at"] < LIVE_URL_TTL:
        return e.get("title"), e.get("year"), e.get("mtype")
    if e and e.get("miss") and now - e["at"] < MISS_TTL:
        return None, None, None
    try:
        url = (f"https://api.themoviedb.org/3/find/{imdb_id}"
               f"?api_key={TMDB_API_KEY}&external_source=imdb_id")
        data = _api_fetch_json(url)
        title = year = mtype = None
        mr = data.get("movie_results") or []
        tr = data.get("tv_results") or []
        if mr:
            title = mr[0].get("title")
            rd = mr[0].get("release_date") or ""
            year = rd[:4] if len(rd) >= 4 else None
            mtype = "movie"
        elif tr:
            title = tr[0].get("name")
            rd = tr[0].get("first_air_date") or ""
            year = rd[:4] if len(rd) >= 4 else None
            mtype = "series"
        if not title:
            raise ValueError("no tmdb match")
        idx[ck] = {"at": now, "title": title, "year": year,
                   "mtype": mtype}
        _sub_index_save(idx)
        return title, year, mtype
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None, None, None


def _titles_match(want, have, year=None):
    """Fuzzy title match: all significant words of `want` in `have`,
    plus year match when both known."""
    w = _norm_title(want)
    h = _norm_title(have)
    if not w or not h:
        return False
    # year check
    if year:
        ym = re.search(r"\b(19|20)\d{2}\b", h)
        if ym and ym.group(0) != str(year):
            return False
    ww = [x for x in w.split() if len(x) > 2]
    if not ww:
        return w in h
    hit = sum(1 for x in ww if x in h)
    return hit / len(ww) >= 0.7


def _mm_live_file_url(imdb_id, season=None):
    """Live Movie Mirror subtitle lookup via WP REST API.

    Chain: TMDB title -> WP search -> post page -> ?custom_download=
    SRT URL. Returns (file_url, referer) or None. Results cached.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"mm_live:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < LIVE_URL_TTL:
            return e["url"], e.get("referer")
    try:
        title, year, _mtype = _tmdb_title(imdb_id)
        if not title:
            raise ValueError("no tmdb title")
        q = urllib.parse.quote(title)
        data = _api_fetch_json(
            f"{MM_WP_API}/search?search={q}&per_page=10")
        post_url = None
        for r in data:
            rt = html.unescape(r.get("title") or "")
            if not _titles_match(title, rt, year):
                continue
            if season is not None:
                blob = _norm_title(rt)
                s2 = "%02d" % season
                if not (f"season {season}" in blob or f"s{s2}" in blob
                        or f"season{s2}" in blob):
                    continue
            # fetch full post to get canonical link
            p = _api_fetch_json(
                f"{MM_WP_API}/posts/{r.get('id')}")
            post_url = p.get("link")
            break
        if not post_url:
            raise ValueError("no wp match")
        page = _sub_fetch(post_url, timeout=15, retries=2)
        page_html = page.decode("utf-8", errors="replace")
        m = re.search(r"custom_download=(https?[^&\"']+)", page_html)
        if not m:
            raise ValueError("no download link")
        file_url = urllib.parse.unquote(m.group(1))
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": post_url}
    _sub_index_save(idx)
    return file_url, post_url


def _goat_live_file_url(imdb_id, season=None):
    """Live Team GOAT subtitle lookup.

    Chain: TMDB title -> homepage title list -> /release/ page ->
    wp.malayalamsubtitles.in/download/<id> SRT URL.
    Returns (file_url, referer) or None. Results cached.
    """
    idx = _sub_index_load()
    now = time.time()
    ck = f"goat_live:{imdb_id}:{season}"
    e = idx.get(ck)
    if e:
        if e.get("miss") and now - e["at"] < MISS_TTL:
            return None
        if e.get("url") and now - e["at"] < LIVE_URL_TTL:
            return e["url"], e.get("referer")
    try:
        title, year, _mtype = _tmdb_title(imdb_id)
        if not title:
            raise ValueError("no tmdb title")
        # homepage embeds all ~500 titles; cache it 1h
        hck = "goat_home_html"
        he = idx.get(hck)
        home_html = None
        if he and he.get("html") and now - he["at"] < 3600:
            home_html = he["html"]
        else:
            raw = _sub_fetch(GOAT_HOME, timeout=15, retries=2)
            home_html = raw.decode("utf-8", errors="replace")
            idx[hck] = {"at": now, "html": home_html[:600000]}
            _sub_index_save(idx)
        release_url = None
        pat = re.compile(
            r'<a href="(/release/[^"]+)"><p class="movie-name name">'
            r"([^<]{3,120})</p>", re.I)
        for m in pat.finditer(home_html):
            slug, t = m.group(1), html.unescape(m.group(2))
            if not _titles_match(title, t, year):
                continue
            if season is not None:
                blob = _norm_title(t)
                s2 = "%02d" % season
                if not (f"season {season}" in blob or f"s{s2}" in blob
                        or f"season{s2}" in blob):
                    continue
            release_url = urllib.parse.urljoin(GOAT_HOME, slug)
            break
        if not release_url:
            raise ValueError("no goat match")
        page = _sub_fetch(release_url, timeout=15, retries=2)
        page_html = page.decode("utf-8", errors="replace")
        dm = re.search(
            r"https://wp\.malayalamsubtitles\.in/download/\d+/?",
            page_html)
        if not dm:
            raise ValueError("no download link")
        file_url = dm.group(0)
    except Exception:
        idx[ck] = {"at": now, "miss": True}
        _sub_index_save(idx)
        return None
    idx[ck] = {"at": now, "url": file_url, "referer": release_url}
    _sub_index_save(idx)
    return file_url, release_url


def _has_subtitle(src, imdb_id, season=None):
    """Fast index-only check: does this source list a subtitle?

    Falls back to live site search (cached) when the saved index
    misses, so newly posted subtitles appear without waiting for
    the next data refresh.
    """
    if src == "mm":
        e = mm_by_tt().get(imdb_id)
        if e:
            item = _pick_series(e, season)
            if item and item.get("sub_url"):
                return True
        # live fallback
        return bool(_mm_live_file_url(imdb_id, season))
    elif src == "goat":
        e = goat_by_tt().get(imdb_id)
        if e:
            item = _pick_series(e, season)
            if item and item.get("sub_url"):
                return True
        # live fallback
        return bool(_goat_live_file_url(imdb_id, season))
    elif src == "msone":
        # Msone needs a live check (Cloudflare) — the file URL doubles
        # as the availability signal.
        return bool(_msone_file_url(imdb_id, season))
    else:
        return False


SUB_SOURCES = [
    ("mm", "Movie Mirror"),
    ("goat", "Team GOAT"),
    ("msone", "Msone"),
]


def _msone_official_entries(vtype, rid):
    """Fetch subtitles from the official Msone addon (pass-through URLs).

    The official addon uses download-manager URLs that bypass Cloudflare,
    so this works for Msone-only titles our local data can't fetch.
    URLs are signed and may expire — always fetch fresh, never cache.
    Tries curl first, then Python urllib as fallback (different TLS
    fingerprint in case Cloudflare blocks one client).
    """
    url = (f"{MSONE_OFFICIAL_API}/subtitles/{vtype}/"
           f"{urllib.parse.quote(rid)}.json")
    data = None
    # attempt 1: curl
    try:
        cmd = ["curl", "-sL", "--max-time", "20", "-A", UA, url]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout:
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception:
        data = None
    # attempt 2: python urllib (different TLS fingerprint)
    if data is None:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            data = None
    if not data:
        return []
    try:
        out = []
        for s in data.get("subtitles", []):
            surl = s.get("url")
            if not surl:
                continue
            out.append({
                "id": f"smsub:msone_off:{rid}:{s.get('id', '')}",
                "url": surl,
                "lang": s.get("lang", "mal"),
            })
        return out
    except Exception:
        return []


@app.route("/debug/msone/<rid>")
def debug_msone(rid):
    """Diagnostic: test official Msone addon connectivity from this server."""
    url = (f"{MSONE_OFFICIAL_API}/subtitles/movie/"
           f"{urllib.parse.quote(rid)}.json")
    info = {"url": url}
    # curl attempt
    try:
        cmd = ["curl", "-sL", "--max-time", "15", "-A", UA, "-w",
               "\n%{http_code}", url]
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        out = r.stdout.decode("utf-8", errors="replace")
        info["curl_rc"] = r.returncode
        info["curl_http"] = out.strip().split("\n")[-1] if out else None
        info["curl_bytes"] = len(r.stdout)
        info["curl_stderr"] = r.stderr.decode("utf-8",
                                              errors="replace")[:200]
    except Exception as e:
        info["curl_error"] = str(e)[:200]
    # urllib attempt
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        info["urllib_http"] = resp.status
        info["urllib_bytes"] = len(raw)
    except Exception as e:
        info["urllib_error"] = str(e)[:200]
    return jsonify(info)




# ---------------- routes ----------------
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "com.smmal.subtitles",
        "version": "1.2.1",
        "name": "SM MAL SUB",
        "description": "Malayalam subtitles from Movie Mirror + Team GOAT "
                       "+ Msone (official addon). Live search: new subtitles "
                       "appear automatically. Subtitles only — video "
                       "comes from your own sources.",
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "catalogs": [],
    })


def _parse_rid(vtype, rid):
    """Returns (imdb_id, season, episode) or None."""
    rid = urllib.parse.unquote(rid)
    parts = rid.split(":")
    imdb_id = parts[0]
    if not re.match(r"^tt\d+$", imdb_id):
        return None
    season = episode = None
    if vtype == "series" and len(parts) >= 3:
        try:
            season, episode = int(parts[1]), int(parts[2])
        except ValueError:
            return None
    return imdb_id, season, episode


def _subtitle_entries(vtype, rid):
    parsed = _parse_rid(vtype, rid)
    if not parsed:
        return []
    imdb_id, season, _episode = parsed
    base = request.url_root.rstrip("/")
    out = []
    idx = None
    for src, _label in SUB_SOURCES:
        if not _has_subtitle(src, imdb_id, season):
            continue
        key = _sub_key(src, rid)
        # remember rid -> key so /srt can resolve lazily on first hit
        if idx is None:
            idx = _sub_index_load()
        if ("rid:" + key) not in idx:
            idx["rid:" + key] = urllib.parse.unquote(rid)
            _sub_index_save(idx)
        out.append({
            "id": f"smsub:{src}:{rid}",
            "url": f"{base}/srt/{src}/{key}.srt",
            "lang": "mal",
        })
    # Official Msone addon (pass-through) — covers Msone-only titles
    # that Cloudflare blocks us from fetching directly.
    out.extend(_msone_official_entries(vtype, rid))
    return out


def _serve_srt(src, key):
    if src not in ("mm", "goat", "msone"):
        return jsonify({"error": "not found"}), 404
    if not re.match(r"^[A-Za-z0-9_]+$", key):
        return jsonify({"error": "not found"}), 404
    if not key.startswith(src + "_"):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(SUB_CACHE_DIR, key + ".srt")
    if not os.path.exists(path):
        # first hit: resolve rid back from the mapping stored at
        # subtitles time, download, and cache
        rid = _sub_index_load().get("rid:" + key)
        if not rid:
            return jsonify({"error": "not found"}), 404
        parsed = _parse_rid("series", rid)
        if not parsed:
            return jsonify({"error": "not found"}), 404
        imdb_id, season, episode = parsed
        if not _resolve_subtitle(src, rid, imdb_id, season, episode):
            return jsonify({"error": "not found"}), 404
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="text/plain; charset=utf-8",
                    headers={"Access-Control-Allow-Origin": "*"})


@app.route("/subtitles/<vtype>/<rid>.json")
def subtitles(vtype, rid):
    return jsonify({"subtitles": _subtitle_entries(vtype, rid)})


@app.route("/srt/<src>/<key>.srt")
def srt(src, key):
    return _serve_srt(src, key)


@app.route("/")
def index():
    base = request.host_url.rstrip("/")
    return Response(
        "<h2>SM MAL SUB by Nandu10 \u2705</h2>"
        "<p>Malayalam subtitles from Msone + Movie Mirror + Team GOAT.</p>"
        "<p>Install in Stremio / Nuvio:<br>"
        f"<code>{base}/manifest.json</code></p>",
        mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
