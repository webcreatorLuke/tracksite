# Board — multi-carrier package tracker

Enter a tracking number, get results from whichever carrier it belongs to
(USPS, UPS, FedEx, OnTrac, DHL, and 1,700+ others), auto-detected via the
17TRACK API.

## Run it locally (demo mode, no API key needed)

```bash
cd tracksite
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. Without an API key the server runs in **demo
mode** — try tracking number `DEMO123` (shows an in-transit UPS package)
or `DEMO404` (shows the not-found state). This lets you see and tweak the
whole UI before you have a real key.

## Go live with real tracking data

1. Create a free account at https://api.17track.net (as of Jan 2026, new
   accounts get a one-time allocation of 200 free tracking numbers — no
   longer a monthly refresh, so budget accordingly if this gets real
   traffic).
2. Find your API token under **Settings → Security → Access Key**.
3. Set it as an environment variable before starting the server:

   ```bash
   export TRACK17_API_KEY="your_key_here"
   python app.py
   ```

   `DEMO_MODE` turns off automatically as soon as this is set.

## How a lookup works

1. Number comes in from the frontend.
2. Server checks its local cache (20 min TTL) — if someone already
   searched this number recently, it's served instantly with no API
   call.
3. If not cached, the server registers the number with 17TRACK, then
   polls for results for a few seconds (brand-new numbers can take a
   moment to populate).
4. The response gets normalized into a simple shape (`carrier`,
   `status`, `events[]`) and cached.

## Things worth doing before real traffic hits this

- **Verify the response shape.** I mapped the parsing in
  `normalize_track17_response()` from 17TRACK's v2.2 docs, but carrier
  data is inconsistent in practice — once you have a real key, track a
  few real numbers, print the raw JSON, and adjust field paths if
  anything doesn't line up.
- **Move the cache off SQLite** if you expect meaningful concurrent
  traffic (Postgres or Redis).
- **Rate limiting** is currently a simple per-IP-per-minute counter
  (10/min) stored in the same SQLite file — fine for a small site, not
  bulletproof against abuse. Consider adding a CAPTCHA (e.g. Cloudflare
  Turnstile) on the form if this gets shared publicly.
- **Webhooks**: 17TRACK can push status updates to a webhook instead of
  you polling — worth switching to if you want "package moved" to
  update your DB without the user re-searching. Not wired up here since
  it needs a publicly reachable URL to register.
- **Deployment**: this is a standard Flask app — deploy behind gunicorn
  + nginx, or on Render/Railway/Fly.io/PythonAnywhere, whatever you're
  comfortable with.

## Files

- `app.py` — Flask backend, 17TRACK integration, cache, rate limiting
- `index.html` — the frontend (single file, vanilla JS, no build step)
- `requirements.txt` — Python deps
