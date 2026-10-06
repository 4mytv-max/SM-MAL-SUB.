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
        elif src == "goat":
            e = goat_by_tt().get(imdb_id)
            item = _pick_series(e, season) if e else None
            if item and item.get("sub_url"):
                file_url = item["sub_url"]
                referer = item.get("post_url")
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


def _has_subtitle(src, imdb_id, season=None):
    """Fast index-only check: does this source list a subtitle?"""
    if src == "mm":
        e = mm_by_tt().get(imdb_id)
    elif src == "goat":
        e = goat_by_tt().get(imdb_id)
    elif src == "msone":
        # Msone needs a live check (Cloudflare) — the file URL doubles
        # as the availability signal.
        return bool(_msone_file_url(imdb_id, season))
    else:
        return False
    if not e:
        return False
    item = _pick_series(e, season)
    return bool(item and item.get("sub_url"))


SUB_SOURCES = [
    ("mm", "Movie Mirror"),
    ("goat", "Team GOAT"),
    ("msone", "Msone"),
]




# ---------------- routes ----------------
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "com.smmal.subtitles",
        "version": "1.0.0",
        "name": "SM MAL SUB",
        "description": "Malayalam subtitles from Msone + Movie Mirror + "
                       "Team GOAT. Subtitles only — video comes from "
                       "your own sources.",
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
