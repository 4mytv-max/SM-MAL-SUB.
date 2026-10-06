# SM MAL SUB by Nandu10

Malayalam subtitles from **Msone + Movie Mirror + Team GOAT** —
subtitles only, no catalog. Video comes from your own sources.

## Install

Stremio / Nuvio → Addons → paste:

```
https://<your-service>.onrender.com/manifest.json
```

## What it does

For every movie/series you watch, lists one Malayalam subtitle entry per
site that carries it:

- `smsub:mm:…` — Movie Mirror
- `smsub:goat:…` — Team GOAT
- `smsub:msone:…` — Msone

Subtitle files download on first use and are cached on disk (`sub_cache/`),
so repeat views are instant.

Note: Msone post pages sit behind Cloudflare which often blocks automated
fetches — the Msone entry is listed only when reachable, so you never see
dead subtitle entries.

## Files

```
app.py
requirements.txt
data/mm_data.json      Movie Mirror items (subtitle URLs)
data/goat_data.json    Team GOAT items (subtitle URLs)
data/mzone_data.json   Msone items (post URLs)
```

## Deploy (Render, free)

1. Push this folder to a GitHub repo.
2. Render → New → Web Service → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
3. Open `https://<your-service>.onrender.com/` for the manifest URL.

No API keys or environment variables needed.
