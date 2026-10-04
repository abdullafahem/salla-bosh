"""
Salla Bosh - web server.

    python app.py                 -> http://localhost:5000
    gunicorn app:app              -> production (Render, Koyeb, ...)

The timetable is scraped from the university site on startup (if there is no
fresh copy) and again every REFRESH_HOURS hours, in a background thread.
"""
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory

import scraper

app = Flask(__name__, static_folder="static")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "12"))
MANUAL_COOLDOWN = 15 * 60  # seconds between manual refreshes

_lock = threading.Lock()
state = {"running": False, "last_error": None, "last_attempt": 0.0}


def data_age_hours():
    if not scraper.OUT_FILE.exists():
        return None
    return (time.time() - scraper.OUT_FILE.stat().st_mtime) / 3600


def _scrape():
    try:
        scraper.run()
        state["last_error"] = None
    except Exception as e:  # noqa: BLE001  (keep the old data if the scrape fails)
        state["last_error"] = str(e)
        print(f"[scrape failed] {e}")
    finally:
        state["running"] = False


def start_scrape():
    with _lock:
        if state["running"]:
            return False
        state["running"] = True
        state["last_attempt"] = time.time()
    threading.Thread(target=_scrape, daemon=True).start()
    return True


def maybe_refresh():
    age = data_age_hours()
    stale = age is None or age > REFRESH_HOURS
    # after a failure, wait 30 min before trying again automatically
    if stale and time.time() - state["last_attempt"] > 30 * 60:
        start_scrape()


@app.get("/")
def index():
    maybe_refresh()
    return send_from_directory(app.static_folder, "index.html")


@app.get("/data.json")
def data():
    maybe_refresh()
    if not scraper.OUT_FILE.exists():
        return jsonify(status="scraping" if state["running"] else "error",
                       error=state["last_error"]), 503
    resp = send_from_directory(scraper.OUT_FILE.parent, scraper.OUT_FILE.name, max_age=300)
    return resp


@app.get("/api/status")
def status():
    age = data_age_hours()
    return jsonify(running=state["running"], last_error=state["last_error"],
                   data_age_hours=None if age is None else round(age, 2),
                   now=datetime.now(timezone.utc).isoformat(timespec="seconds"))


@app.post("/api/refresh")
def refresh():
    if time.time() - state["last_attempt"] < MANUAL_COOLDOWN:
        return jsonify(started=False, reason="Rifreskimi u bë së fundi. Provo pas pak minutash."), 429
    return jsonify(started=start_scrape())


maybe_refresh()  # scrape on boot when there is no fresh data

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
