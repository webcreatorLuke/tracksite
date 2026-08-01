"""
Multi-carrier package tracking site — backend.

Uses the 17TRACK API (https://api.17track.net) to auto-detect the carrier
for a tracking number and pull its full event history, so the user never
has to say which carrier they used.

Flow for a lookup:
  1. Check local cache (avoids re-hitting the API for a number someone
     just searched).
  2. Register the number with 17TRACK (POST /register). This tells
     17TRACK to start tracking it if it hasn't seen it before.
  3. Query the current info (POST /gettrackinfo). Fresh registrations can
     take a few seconds to a few minutes to populate, so we retry briefly.
  4. Normalize whatever comes back into a simple shape the frontend can
     render, and cache it.

Set the TRACK17_API_KEY environment variable before running for real.
Without it, the server runs in DEMO MODE and returns realistic sample
data for a few magic tracking numbers (see DEMO_DATA below) so the UI
can be built and tested before you have a key.
"""

import os
import time
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, g, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "cache.db")

TRACK17_API_KEY = os.environ.get("TRACK17_API_KEY", "").strip()
TRACK17_BASE = "https://api.17track.net/track/v2.2"
DEMO_MODE = TRACK17_API_KEY == ""

CACHE_TTL_MINUTES = 20
# Basic per-IP throttling for a public-facing endpoint.
RATE_LIMIT_PER_MINUTE = 10

app = Flask(__name__, static_folder="static", static_url_path="/static")

# --------------------------------------------------------------------------
# Storage: a tiny sqlite cache + rate-limit table. Fine for a small site;
# swap for Postgres/Redis if this gets real traffic.
# --------------------------------------------------------------------------

_db_lock = threading.Lock()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS lookups (
                number TEXT PRIMARY KEY,
                carrier TEXT,
                status TEXT,
                payload TEXT,
                fetched_at TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit (
                ip TEXT,
                window_start TEXT,
                count INTEGER,
                PRIMARY KEY (ip, window_start)
            )
            """
        )
        db.commit()


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def check_rate_limit(ip: str) -> bool:
    """Returns True if the request is allowed."""
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    db = get_db()
    with _db_lock:
        row = db.execute(
            "SELECT count FROM rate_limit WHERE ip = ? AND window_start = ?",
            (ip, window),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO rate_limit (ip, window_start, count) VALUES (?, ?, 1)",
                (ip, window),
            )
            db.commit()
            return True
        if row["count"] >= RATE_LIMIT_PER_MINUTE:
            return False
        db.execute(
            "UPDATE rate_limit SET count = count + 1 WHERE ip = ? AND window_start = ?",
            (ip, window),
        )
        db.commit()
        return True


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def get_cached(number: str):
    db = get_db()
    row = db.execute(
        "SELECT payload, fetched_at FROM lookups WHERE number = ?", (number,)
    ).fetchone()
    if row is None:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    if datetime.now(timezone.utc) - fetched_at > timedelta(minutes=CACHE_TTL_MINUTES):
        return None
    import json

    return json.loads(row["payload"])


def set_cached(number: str, carrier: str, status: str, payload: dict):
    import json

    db = get_db()
    db.execute(
        """
        INSERT INTO lookups (number, carrier, status, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(number) DO UPDATE SET
            carrier=excluded.carrier,
            status=excluded.status,
            payload=excluded.payload,
            fetched_at=excluded.fetched_at
        """,
        (number, carrier, status, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


# --------------------------------------------------------------------------
# 17TRACK integration
# --------------------------------------------------------------------------


def track17_headers():
    return {"17token": TRACK17_API_KEY, "Content-Type": "application/json"}


def register_number(number: str):
    resp = requests.post(
        f"{TRACK17_BASE}/register",
        headers=track17_headers(),
        json=[{"number": number}],
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_track_info(number: str):
    resp = requests.post(
        f"{TRACK17_BASE}/gettrackinfo",
        headers=track17_headers(),
        json=[{"number": number}],
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def normalize_track17_response(number: str, raw: dict) -> dict:
    """
    17TRACK's v2.2 response shape (see api.17track.net/en/doc). We defend
    against missing fields since carriers report inconsistent detail.
    Adjust the field paths here once you've inspected a real response
    for your account — the exact nesting can shift between API versions.
    """
    accepted = (raw.get("data") or {}).get("accepted") or []
    if not accepted:
        return {
            "number": number,
            "found": False,
            "status": "not_found",
            "message": "No tracking data yet — the number may still be registering, "
            "or it wasn't recognized by any carrier.",
        }

    entry = accepted[0]
    track_info = entry.get("track_info") or {}
    latest_status = track_info.get("latest_status") or {}
    tracking = track_info.get("tracking") or {}
    providers = tracking.get("providers") or []

    events = []
    for provider in providers:
        provider_name = (provider.get("provider") or {}).get("name", "")
        for ev in provider.get("events") or []:
            events.append(
                {
                    "time": ev.get("time_iso") or ev.get("time_utc") or ev.get("time"),
                    "description": ev.get("description", ""),
                    "location": ev.get("location", ""),
                    "provider": provider_name,
                }
            )

    # Most recent first
    events.sort(key=lambda e: e.get("time") or "", reverse=True)

    carrier_name = (entry.get("carrier_key") or entry.get("carrier") or "Unknown carrier")

    return {
        "number": number,
        "found": True,
        "carrier": carrier_name,
        "status": latest_status.get("status", "unknown"),
        "sub_status": latest_status.get("sub_status", ""),
        "events": events,
    }


def lookup_live(number: str) -> dict:
    register_number(number)

    # Fresh registrations can take a moment to populate. Poll briefly.
    raw = None
    for attempt in range(4):
        raw = fetch_track_info(number)
        accepted = (raw.get("data") or {}).get("accepted") or []
        if accepted and (accepted[0].get("track_info") or {}).get("latest_status"):
            break
        time.sleep(2 * (attempt + 1))

    return normalize_track17_response(number, raw or {})


# --------------------------------------------------------------------------
# Demo data — lets you build/test the frontend before you have an API key
# --------------------------------------------------------------------------

DEMO_DATA = {
    "DEMO123": {
        "number": "DEMO123",
        "found": True,
        "carrier": "UPS",
        "status": "InTransit",
        "sub_status": "InTransit_PickedUp",
        "events": [
            {"time": "2026-07-30T14:22:00Z", "description": "Departed from facility",
             "location": "Louisville, KY", "provider": "UPS"},
            {"time": "2026-07-29T09:10:00Z", "description": "Arrived at sorting facility",
             "location": "Louisville, KY", "provider": "UPS"},
            {"time": "2026-07-28T18:45:00Z", "description": "Picked up",
             "location": "Concord, CA", "provider": "UPS"},
        ],
    },
    "DEMO404": {
        "number": "DEMO404",
        "found": False,
        "status": "not_found",
        "message": "No tracking data yet — the number may still be registering, "
        "or it wasn't recognized by any carrier.",
    },
}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/api/track", methods=["POST"])
def api_track():
    body = request.get_json(silent=True) or {}
    number = (body.get("number") or "").strip()

    if not number:
        return jsonify({"error": "Enter a tracking number."}), 400
    if len(number) > 40:
        return jsonify({"error": "That doesn't look like a tracking number."}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    if not check_rate_limit(ip):
        return jsonify({"error": "Too many lookups — wait a minute and try again."}), 429

    cached = get_cached(number)
    if cached:
        return jsonify({**cached, "cached": True})

    if DEMO_MODE:
        result = DEMO_DATA.get(number.upper()) or {
            "number": number,
            "found": False,
            "status": "not_found",
            "message": "DEMO MODE: try DEMO123 (in transit) or DEMO404 (not found). "
            "Set TRACK17_API_KEY to look up real tracking numbers.",
        }
        set_cached(number, result.get("carrier", ""), result.get("status", ""), result)
        return jsonify({**result, "demo_mode": True})

    try:
        result = lookup_live(number)
    except requests.HTTPError as e:
        return jsonify({"error": f"Carrier lookup failed ({e.response.status_code})."}), 502
    except requests.RequestException:
        return jsonify({"error": "Couldn't reach the tracking service. Try again shortly."}), 502

    set_cached(number, result.get("carrier", ""), result.get("status", ""), result)
    return jsonify(result)


if __name__ == "__main__":
    init_db()
    if DEMO_MODE:
        print("⚠ Running in DEMO MODE — no TRACK17_API_KEY set. Try tracking number DEMO123.")
    app.run(debug=True, port=5000)
