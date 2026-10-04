# Salla Bosh

Find the free rooms and labs at the university right now, or at any hour of the week.
The data comes live from the official timetable at
`http://37.139.119.36:81/orari/pedagog` and `/orari/student`.

## Run it on your computer

```bash
pip install -r requirements.txt
python scraper.py --inspect     # 1. check that the site is read correctly
python scraper.py               # 2. download the full timetable -> data/timetable.json
python app.py                   # 3. open http://localhost:5000
```

`python app.py` alone also works: it scrapes on startup if there is no data,
then again every 12 hours (`REFRESH_HOURS`).

## How it works

`scraper.py` opens both timetable pages, then reads every teacher / group in
the dropdowns (forms, JavaScript dropdowns, links, iframes and AJAX endpoints
are all handled). Every lesson is matched to a room. Rooms whose name contains
"lab" are shown as **Laboratorë**, all others keep their own name
(Salla 205, Auditori A, Klasa 3...). The web page then shows, for the chosen
day and hour, which rooms have no lesson and until when they stay free.

## If rooms are missing or wrongly named

Edit `rooms_config.json`:

* `aliases`: merge two spellings, e.g. `{"S 205": "Salla 205"}`
* `ignore`: things that are not real rooms, e.g. `"Online"`
* `extra_patterns`: your own regex for room names (first group = room name),
  e.g. `"(Kati\\s*\\d+\\s*-\\s*\\d+)"`
* `lab_keywords`: words that mark a room as a lab

If `--inspect` shows "Nothing parsed", the files in `debug/` show the exact
page structure, so the parser can be adapted to it.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ORARI_BASE_URL` | `http://37.139.119.36:81` | timetable server |
| `ORARI_PATHS` | `/orari/pedagog,/orari/student` | pages to read |
| `ORARI_DELAY` | `0.25` | seconds between requests (be gentle with the uni server) |
| `REFRESH_HOURS` | `12` | how often the web app re-scrapes |

## Free hosting

**Option A: GitHub Pages (recommended, never sleeps).** Push this folder to a
GitHub repo, then in *Settings → Pages* choose *Source: GitHub Actions*.
`.github/workflows/pages.yml` scrapes every `REFRESH_HOURS` hours (default 12)
and publishes the site. To change it, add a repository variable `REFRESH_HOURS`
in *Settings → Secrets and variables → Actions → Variables*. Every push and
*Actions → Run workflow* scrape immediately.

**Option B: Render (full Flask app with the refresh button).** Create a new
*Blueprint* on render.com from your repo; `render.yaml` sets everything up.
The free plan sleeps after ~15 min without visits, so the first visit after
that is slow and may trigger a new scrape.

If the university server refuses connections from foreign servers (GitHub and
Render run in the US/EU), run `python scraper.py` on your own computer, copy
`data/timetable.json` to the repo as `static/data.json`, and serve `static/`
on GitHub Pages without the workflow.
