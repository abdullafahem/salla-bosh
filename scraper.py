#!/usr/bin/env python3
"""
Scraper for the university timetable ("orari") -> data/timetable.json

It reads the real pages (by default /orari/pedagog and /orari/student),
follows every dropdown option / link / iframe / AJAX endpoint it finds,
parses the timetable tables (grid or list layout), extracts the rooms
(labs vs. normal rooms) and writes one JSON file the web app uses.

Usage:
    python scraper.py              # scrape everything, write data/timetable.json
    python scraper.py --inspect    # show what the site looks like (debug), saves HTML in debug/
    python scraper.py --limit 3    # quick test: only 3 options per dropdown
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
BASE_URL = os.environ.get("ORARI_BASE_URL", "http://37.139.119.36:81")
START_PATHS = [p.strip() for p in os.environ.get("ORARI_PATHS", "/orari/pedagog,/orari/student").split(",") if p.strip()]
OUT_FILE = Path(os.environ.get("ORARI_OUT", HERE / "data" / "timetable.json"))
CONFIG_FILE = Path(os.environ.get("ORARI_CONFIG", HERE / "rooms_config.json"))
DELAY = float(os.environ.get("ORARI_DELAY", "0.25"))          # be polite to the uni server
MAX_REQUESTS = int(os.environ.get("ORARI_MAX_REQUESTS", "2500"))
TIMEOUT = 25

DAY_NAMES = ["E Hënë", "E Martë", "E Mërkurë", "E Enjte", "E Premte", "E Shtunë", "E Diel"]


# --------------------------------------------------------------------------- text helpers
def norm(s: str) -> str:
    """lowercase, remove accents (ë -> e, ç -> c), collapse spaces"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()


def cell_text(cell) -> str:
    if cell is None:
        return ""
    for br in cell.find_all("br"):
        br.replace_with("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in cell.get_text("\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


DAY_WORDS = {
    0: ["hene", "monday"], 1: ["marte", "tuesday"], 2: ["merkure", "wednesday"],
    3: ["enjte", "thursday"], 4: ["premte", "friday"], 5: ["shtune", "saturday"], 6: ["diel", "sunday"],
}
DAY_ABBR = {"he": 0, "hen": 0, "ma": 1, "mar": 1, "me": 2, "mer": 2, "en": 3, "enj": 3,
            "pr": 4, "pre": 4, "sh": 5, "sht": 5, "mon": 0, "tue": 1, "wed": 2,
            "thu": 3, "fri": 4, "sat": 5}


def day_of(text: str):
    n = norm(text)
    if not n or len(n) > 40:
        return None
    for d, words in DAY_WORDS.items():
        for w in words:
            if re.search(r"\b" + w, n):
                return d
    return DAY_ABBR.get(n.strip(" .:"))


RANGE_RE = re.compile(r"(\d{1,2})\s*[:.h]\s*(\d{2})\s*(?:-|–|—|deri|to|/)\s*(\d{1,2})\s*[:.h]\s*(\d{2})")
SINGLE_RE = re.compile(r"^\D{0,12}?(\d{1,2})\s*[:.]\s*(\d{2})\D{0,6}$")


def time_of(text: str):
    """returns (start_minutes, end_minutes_or_None) or None"""
    t = (text or "").strip()
    if not t or len(t) > 40:
        return None
    m = RANGE_RE.search(t)
    if m:
        a = int(m[1]) * 60 + int(m[2])
        b = int(m[3]) * 60 + int(m[4])
        if 0 <= a < b <= 24 * 60:
            return a, b
    m = SINGLE_RE.match(t)
    if m:
        a = int(m[1]) * 60 + int(m[2])
        if 6 * 60 <= a <= 22 * 60:
            return a, None
    return None


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- rooms
def load_config() -> dict:
    cfg = {"extra_patterns": [], "lab_keywords": ["lab"], "ignore": [], "aliases": {}, "bare_codes": True}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not read {CONFIG_FILE}: {e}", file=sys.stderr)
    return cfg


CFG = load_config()

ID = r"[A-Za-z]?\s?-?\s?\d{1,3}[A-Za-z]?"
ROOM_PATTERNS = [
    # Lab 3, Lab. A2, Laboratori 4, Laboratori Informatike 3
    (re.compile(r"\b(lab(?:oratori(?:um|t)?)?)\.?\s*((?:[A-Za-zËëÇç]{3,}\s+){0,2}?" + ID + r")(?![\w])", re.I), "lab"),
    # Laboratori i Kimisë  (lab with a name, no number)
    (re.compile(r"\b(lab(?:oratori(?:um|t)?)?)\.?\s+(?:i|e|of)\s+([A-ZËÇ][\wëç]+)", re.I), "lab"),
    # Salla 205, Klasa 3, Aula B2, Auditori 1, Aud. A
    (re.compile(r"\b(salla|salle|klasa|aula|auditori(?:um|t)?|aud)\.?\s*[:.]?\s*(" + ID + r"|[A-Z]{1,2})(?![\w])", re.I), "salla"),
]
LABEL_RE = re.compile(r"(?:salla|klasa|ambienti|auditori|room)\s*:\s*([^\n,;|]+)", re.I)
BARE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2}\s?-?\d{2,3}[A-Za-z]?)(?![A-Za-z0-9])")


def is_lab(name: str) -> bool:
    n = norm(name)
    return any(k in n for k in CFG.get("lab_keywords", ["lab"]))


def canon_room(keyword: str, ident: str) -> str:
    ident = re.sub(r"\s*-\s*", "-", re.sub(r"\s+", " ", ident.strip(" .:-")))
    if re.fullmatch(r"[a-z]{1,2}-?\d.*|\d+[a-z]?", ident, re.I):
        ident = ident.upper()
    else:
        ident = " ".join(w if w.isupper() or any(c.isdigit() for c in w) else w.capitalize() for w in ident.split())
    k = norm(keyword)
    if k.startswith("lab"):
        word = "Lab"
    elif k.startswith("aud"):
        word = "Auditori"
    else:
        word = keyword.strip().capitalize()
    return f"{word} {ident}".strip()


def finalize_room(name: str):
    name = re.sub(r"\s+", " ", name).strip(" .,:;-")
    if not name or len(name) > 40:
        return None
    name = CFG.get("aliases", {}).get(name, name)
    if any(norm(name) == norm(x) for x in CFG.get("ignore", [])):
        return None
    return name


def rooms_in_text(text: str):
    """returns list of (room_name, matched_span_text)"""
    found = []
    for pat in CFG.get("extra_patterns", []):
        for m in re.finditer(pat, text, re.I):
            found.append((m.group(1) if m.groups() else m.group(0), m.group(0)))
    if not found:
        for pat, _kind in ROOM_PATTERNS:
            for m in pat.finditer(text):
                if any(m.group(0) in f[1] or f[1] in m.group(0) for f in found):
                    continue
                found.append((canon_room(m.group(1), m.group(2)), m.group(0)))
    if not found:
        for m in LABEL_RE.finditer(text):
            found.append((m.group(1), m.group(0)))
    if not found and CFG.get("bare_codes", True):
        for m in BARE_RE.finditer(text):
            found.append((m.group(1).replace(" ", ""), m.group(0)))
    out, seen = [], set()
    for name, span in found:
        name = finalize_room(name)
        if name and name not in seen:
            seen.add(name)
            out.append((name, span))
    return out


TEACHER_RE = re.compile(r"\b(prof|dr|doc|msc|ph\.?\s?d|lektor|pedagog|ass|asist|pr\.)\b\.?", re.I)
TYPE_RE = re.compile(r"\b(leksion|seminar|laborator|lab|praktik[ae]?|ushtrim)\w*", re.I)
SKIP_CELL_RE = re.compile(r"^(pushim|break|-+|—|x|\s*)$", re.I)


def split_lesson(text: str, rooms):
    """from the cell text (rooms removed) guess subject / teacher / type"""
    rest = text
    for _name, span in rooms:
        rest = rest.replace(span, " ")
    lines = [re.sub(r"\s+", " ", ln).strip(" ,;:-|()/") for ln in rest.split("\n")]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"(salla|klasa|lab|auditori)\.?", ln, re.I)]
    teacher = next((ln for ln in lines if TEACHER_RE.search(ln)), "")
    others = [ln for ln in lines if ln != teacher]
    kind = ""
    m = TYPE_RE.search(text)
    if m:
        kind = m.group(0).capitalize()
    subject = others[0] if others else ""
    return subject[:120], teacher[:80], kind


# --------------------------------------------------------------------------- tables
def table_rows(table):
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def to_int(v, default=1):
    try:
        return max(1, min(60, int(str(v).strip() or default)))
    except ValueError:
        return default


def expand_table(table):
    """table -> matrix of cell objects (rowspan/colspan repeated)"""
    grid, nrows, ncols = {}, 0, 0
    for r, tr in enumerate(table_rows(table)):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in grid:
                c += 1
            rs, cs = to_int(cell.get("rowspan")), to_int(cell.get("colspan"))
            for i in range(rs):
                for j in range(cs):
                    grid[(r + i, c + j)] = cell
            c += cs
            ncols = max(ncols, c)
        nrows = max(nrows, r + 1)
    nrows = max([k[0] + 1 for k in grid] + [nrows])
    return [[grid.get((r, c)) for c in range(ncols)] for r in range(nrows)]


def _fill_ends(slots):
    """slots: dict key->(start,end|None). Fill missing ends with next start."""
    starts = sorted({s for s, _ in slots.values()})
    gaps = [b - a for a, b in zip(starts, starts[1:]) if b > a]
    typical = min(gaps) if gaps else 60
    out = {}
    for k, (s, e) in slots.items():
        if e is None:
            later = [x for x in starts if x > s]
            e = later[0] if later else s + typical
        out[k] = (s, e)
    return out


def parse_grid(matrix, texts):
    """Timetable grid: days on one axis, hours on the other."""
    nr = len(matrix)
    nc = len(matrix[0]) if nr else 0
    if nr < 2 or nc < 2:
        return []

    def axis_days_in_row(r):
        return {c: day_of(texts[r][c]) for c in range(nc) if day_of(texts[r][c]) is not None}

    def axis_days_in_col(c):
        return {r: day_of(texts[r][c]) for r in range(nr) if day_of(texts[r][c]) is not None}

    entries = []
    # orientation A: days in a header row, times down a column
    for hr in range(min(6, nr)):
        dcols = axis_days_in_row(hr)
        if len(set(dcols.values())) >= 2:
            row_time = {}
            for r in range(hr + 1, nr):
                for c in range(min(3, nc)):
                    if c in dcols:
                        continue
                    t = time_of(texts[r][c])
                    if t:
                        row_time[r] = t
                        break
            if not row_time:
                break
            row_time = _fill_ends(row_time)
            header_cells = {id(matrix[hr][c]) for c in range(nc)}
            spans = {}
            for c, d in dcols.items():
                for r, (s, e) in row_time.items():
                    cell = matrix[r][c]
                    if cell is None or id(cell) in header_cells:
                        continue
                    key = (id(cell), d)
                    if key in spans:
                        spans[key][1] = min(spans[key][1], s)
                        spans[key][2] = max(spans[key][2], e)
                    else:
                        spans[key] = [texts[r][c], s, e, d]
            for txt, s, e, d in spans.values():
                entries.append((d, s, e, txt))
            return entries
    # orientation B: days down a column, times across a header row
    for hc in range(min(3, nc)):
        drows = axis_days_in_col(hc)
        if len(set(drows.values())) >= 2:
            col_time = {}
            for r in range(min(6, nr)):
                if r in drows:
                    continue
                cand = {c: time_of(texts[r][c]) for c in range(nc) if c != hc and time_of(texts[r][c])}
                if len(cand) >= 2:
                    col_time = cand
                    break
            if not col_time:
                break
            col_time = _fill_ends(col_time)
            spans = {}
            for r, d in drows.items():
                for c, (s, e) in col_time.items():
                    cell = matrix[r][c]
                    if cell is None or cell is matrix[r][hc]:
                        continue
                    key = (id(cell), d)
                    if key in spans:
                        spans[key][1] = min(spans[key][1], s)
                        spans[key][2] = max(spans[key][2], e)
                    else:
                        spans[key] = [texts[r][c], s, e, d]
            for txt, s, e, d in spans.values():
                entries.append((d, s, e, txt))
            return entries
    return []


COLS = {
    "day": ["dita", "day", "dite"],
    "time": ["ora", "orari", "koha", "time", "hour"],
    "start": ["fillim", "start", "nga"],
    "end": ["mbarim", "end", "deri"],
    "room": ["salla", "klasa", "auditor", "room", "ambient", "vendi", "lab"],
    "subject": ["lend", "subject", "kurs", "modul", "disiplin"],
    "teacher": ["pedagog", "lektor", "teacher", "profesor", "docent"],
    "group": ["grup", "group", "paralel"],
    "type": ["tipi", "lloji", "type"],
}


def parse_list(matrix, texts):
    """List layout: one lesson per row with columns like Dita | Ora | Lënda | Salla."""
    for hr in range(min(3, len(texts))):
        cols = {}
        for c, t in enumerate(texts[hr]):
            n = norm(t)
            if not n or len(n) > 30:
                continue
            for key, words in COLS.items():
                if key not in cols and any(n.startswith(w) or f" {w}" in n for w in words):
                    cols[key] = c
                    break
        if "day" in cols and ("time" in cols or "start" in cols):
            out, last_day = [], None
            for r in range(hr + 1, len(texts)):
                row = texts[r]
                get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else ""  # noqa: E731
                d = day_of(get("day"))
                d = last_day if d is None else d
                last_day = d
                if d is None:
                    continue
                if "time" in cols:
                    t = time_of(get("time"))
                    if not t:
                        continue
                    s, e = t
                else:
                    a, b = time_of(get("start")), time_of(get("end"))
                    if not a:
                        continue
                    s, e = a[0], (b[0] if b else a[1])
                if e is None:
                    e = s + 60
                out.append({
                    "day": d, "start": s, "end": e,
                    "room_raw": get("room"), "subject": get("subject"),
                    "teacher": get("teacher"), "group": get("group"), "kind": get("type"),
                    "text": "\n".join(x for x in row if x),
                })
            return out
    return []


def entries_from_html(html: str, context: dict):
    soup = BeautifulSoup(html, "html.parser")
    lessons = []
    for table in soup.find_all("table"):
        matrix = expand_table(table)
        if not matrix:
            continue
        texts = [[cell_text(c) if c is not None else "" for c in row] for row in matrix]
        listed = parse_list(matrix, texts)
        for item in listed:
            raw = item.pop("room_raw")
            rooms = []
            if raw:
                parsed = rooms_in_text(raw)
                first = raw.split("\n")[0].strip()
                if re.fullmatch(r"[A-Za-z]{0,2}\s?-?\d{1,4}[A-Za-z]?", first):
                    first = "Salla " + first.upper()  # bare number -> "Salla 301"
                rooms = [r for r, _ in parsed] or [n for n in [finalize_room(first)] if n]
            else:
                rooms = [r for r, _ in rooms_in_text(item["text"])]
            for room in rooms:
                lessons.append(make_lesson(item["day"], item["start"], item["end"], room,
                                           item["subject"], item["teacher"], item["group"], item["kind"], context))
        if listed:
            continue
        for d, s, e, txt in parse_grid(matrix, texts):
            if not txt or SKIP_CELL_RE.match(txt):
                continue
            rooms = rooms_in_text(txt)
            subject, teacher, kind = split_lesson(txt, rooms)
            for room, _span in rooms:
                lessons.append(make_lesson(d, s, e, room, subject, teacher, "", kind, context))
    return lessons


def make_lesson(day, start, end, room, subject, teacher, group, kind, context):
    if context.get("kind") == "pedagog" and not teacher:
        teacher = context.get("label", "")
    if context.get("kind") == "student" and not group:
        group = context.get("label", "")
    return {"day": day, "start": start, "end": end, "room": room,
            "subject": (subject or "").strip(), "teacher": (teacher or "").strip(),
            "group": (group or "").strip(), "kind": (kind or "").strip()}


def entries_from_json(obj, context):
    """AJAX endpoints sometimes return JSON (HTML inside, or a list of rows)."""
    lessons = []
    if isinstance(obj, str) and "<t" in obj:
        return entries_from_html(obj, context)
    if isinstance(obj, dict):
        for v in obj.values():
            lessons += entries_from_json(v, context)
    elif isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        keys = list({k for x in obj for k in x})
        html = "<table><tr>" + "".join(f"<th>{k}</th>" for k in keys) + "</tr>"
        for x in obj:
            html += "<tr>" + "".join(f"<td>{x.get(k, '')}</td>" for k in keys) + "</tr>"
        lessons += entries_from_html(html + "</table>", context)
    elif isinstance(obj, list):
        for v in obj:
            lessons += entries_from_json(v, context)
    return lessons


# --------------------------------------------------------------------------- crawling
class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (SallaBosh timetable reader)",
                               "Accept-Language": "sq,en;q=0.8"})
        self.count = 0
        self.cache = {}

    def fetch(self, url, method="GET", data=None):
        key = (method, url, json.dumps(data, sort_keys=True) if data else "")
        if key in self.cache:
            return self.cache[key]
        if self.count >= MAX_REQUESTS:
            return None
        self.count += 1
        for attempt in range(3):
            try:
                if method == "POST":
                    r = self.s.post(url, data=data, timeout=TIMEOUT)
                else:
                    r = self.s.get(url, params=data, timeout=TIMEOUT)
                if r.encoding is None or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding or "utf-8"
                time.sleep(DELAY)
                self.cache[key] = r
                return r
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"  [error] {method} {url} {data or ''}: {e}", file=sys.stderr)
                time.sleep(1.5 * (attempt + 1))
        return None


PLACEHOLDER_RE = re.compile(r"^(zgjidh|select|choose|--|—|\.\.\.)", re.I)


def real_options(select):
    opts = []
    for o in select.find_all("option"):
        v = (o.get("value") if o.has_attr("value") else o.get_text()).strip()
        t = o.get_text(" ", strip=True)
        if not v or PLACEHOLDER_RE.match(t) or v in ("0", "-1"):
            continue
        opts.append((v, t))
    return opts


def form_requests(form, page_url, limit):
    action = urljoin(page_url, form.get("action") or page_url)
    method = (form.get("method") or "GET").upper()
    base = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        typ = (inp.get("type") or "text").lower()
        if not name or typ in ("checkbox", "radio", "file", "image", "reset"):
            continue
        if typ == "submit":
            if name not in base:
                base[name] = inp.get("value", "")
            continue
        base[name] = inp.get("value", "")
    btn = form.find("button", attrs={"name": True})
    if btn:
        base.setdefault(btn["name"], btn.get("value", ""))
    selects = [(s.get("name") or s.get("id"), real_options(s)) for s in form.find_all("select")]
    selects = [(n, o) for n, o in selects if n and o]
    if not selects:
        return []
    reqs = []
    total = 1
    for _, o in selects:
        total *= len(o)
    if total <= 1500:
        combos = itertools.product(*[o[:limit] if limit else o for _, o in selects])
        for combo in combos:
            data = dict(base)
            for (name, _), (v, _t) in zip(selects, combo):
                data[name] = v
            reqs.append((action, method, data, " / ".join(t for _v, t in combo)))
    else:  # too many combinations: iterate the biggest dropdown only
        big = max(range(len(selects)), key=lambda i: len(selects[i][1]))
        for v, t in (selects[big][1][:limit] if limit else selects[big][1]):
            data = dict(base)
            for i, (name, o) in enumerate(selects):
                data[name] = v if i == big else o[0][0]
            reqs.append((action, method, data, t))
    return reqs


AJAX_RE = re.compile(r"""(?:url\s*:\s*|\.get\(\s*|\.post\(\s*|\.load\(\s*|fetch\(\s*)['"`]([^'"`]+)['"`]""", re.I)
ONCHANGE_PREFIX_RE = re.compile(r"""['"]([^'"]*)['"]\s*\+\s*(?:this\.value|this\.options|\$\(this\)\.val\(\)|value)""")


def loose_select_requests(soup, select, page_url, fetcher, limit, kind):
    """dropdown outside a <form> (navigation done by JavaScript). Find the URL pattern by probing."""
    name = select.get("name") or select.get("id") or "id"
    opts = real_options(select)
    if limit:
        opts = opts[:limit]
    if not opts:
        return []
    v0 = opts[0][0]
    if v0.startswith(("http", "/", "?", ".")):
        return [(urljoin(page_url, v), "GET", None, t) for v, t in opts]
    patterns = []
    on = select.get("onchange", "") + " " + " ".join(s.get_text() for s in soup.find_all("script"))
    for m in ONCHANGE_PREFIX_RE.finditer(on):
        patterns.append(("GET", urljoin(page_url, m.group(1)) + "{v}", None))
    for m in AJAX_RE.finditer(on):
        u = urljoin(page_url, m.group(1))
        patterns.append(("GET", u, {name: "{v}"}))
        patterns.append(("POST", u, {name: "{v}"}))
        patterns.append(("GET", u.rstrip("/") + "/{v}", None))
    patterns += [("GET", page_url.rstrip("/") + "/{v}", None),
                 ("GET", page_url, {name: "{v}"}),
                 ("GET", page_url, {"id": "{v}"}),
                 ("POST", page_url, {name: "{v}"})]

    def build(p, v):
        method, u, data = p
        return (u.replace("{v}", v), method, {k: x.replace("{v}", v) for k, x in data.items()} if data else None)

    for p in patterns:
        url, method, data = build(p, v0)
        r = fetcher.fetch(url, method, data)
        if r is not None and r.ok and lessons_from_response(r, {"kind": kind, "label": opts[0][1]}):
            print(f"  [found] dropdown '{name}' works with {method} {p[1]} {p[2] or ''}")
            return [(*build(p, v), t) for v, t in opts]
    print(f"  [warn] could not find how dropdown '{name}' loads data (run --inspect)")
    return []


def lessons_from_response(resp, context):
    ctype = resp.headers.get("Content-Type", "")
    text = resp.text
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        try:
            return entries_from_json(resp.json(), context)
        except ValueError:
            pass
    return entries_from_html(text, context)


def page_kind(url):
    p = urlparse(url).path.lower()
    return "pedagog" if "pedagog" in p else "student" if "student" in p else "other"


def crawl(limit=0, verbose=True):
    f = Fetcher()
    all_lessons = []
    for path in START_PATHS:
        url = urljoin(BASE_URL, path)
        kind = page_kind(url)
        print(f"[page] {url}")
        r = f.fetch(url)
        if r is None or not r.ok:
            print(f"  [error] cannot open {url} ({getattr(r, 'status_code', 'no response')})", file=sys.stderr)
            continue
        all_lessons += lessons_from_response(r, {"kind": kind, "label": ""})
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        for form in soup.find_all("form"):
            jobs += form_requests(form, url, limit)
        for sel in soup.find_all("select"):
            if sel.find_parent("form") is None:
                jobs += loose_select_requests(soup, sel, url, f, limit, kind)
        host = urlparse(url).netloc
        links = []
        for a in soup.find_all("a", href=True):
            u = urljoin(url, a["href"]).split("#")[0]
            if urlparse(u).netloc == host and urlparse(u).path.startswith(urlparse(url).path) and u.rstrip("/") != url.rstrip("/"):
                links.append((u, "GET", None, a.get_text(" ", strip=True)))
        jobs += links[:limit] if limit else links
        for fr in soup.find_all(["iframe", "frame"], src=True):
            jobs.append((urljoin(url, fr["src"]), "GET", None, ""))
        print(f"  {len(jobs)} timetables to read")
        done = set()
        for i, (u, method, data, label) in enumerate(jobs, 1):
            k = (u, method, json.dumps(data, sort_keys=True) if data else "")
            if k in done:
                continue
            done.add(k)
            resp = f.fetch(u, method, data)
            if resp is None or not resp.ok:
                continue
            got = lessons_from_response(resp, {"kind": kind, "label": label})
            all_lessons += got
            if verbose and (i % 20 == 0 or i == len(jobs)):
                print(f"  {i}/{len(jobs)} read, {len(all_lessons)} lessons so far")
    print(f"[done] {f.count} requests")
    return all_lessons


# --------------------------------------------------------------------------- output
def build_dataset(lessons):
    merged = {}
    for l in lessons:
        key = (l["day"], l["start"], l["end"], l["room"])
        m = merged.setdefault(key, {**l, "teacher": set(), "group": set(), "subject": set(), "kind": set()})
        for k in ("teacher", "group", "subject", "kind"):
            if l[k]:
                m[k].add(l[k])
    out = []
    for m in merged.values():
        out.append({
            "day": m["day"], "start": hhmm(m["start"]), "end": hhmm(m["end"]), "room": m["room"],
            "subject": " / ".join(sorted(m["subject"])), "teacher": ", ".join(sorted(m["teacher"])),
            "group": ", ".join(sorted(m["group"])), "kind": ", ".join(sorted(m["kind"])),
        })
    out.sort(key=lambda x: (x["day"], x["start"], x["room"]))
    rooms = sorted({l["room"] for l in out}, key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": [urljoin(BASE_URL, p) for p in START_PATHS],
        "day_names": DAY_NAMES,
        "rooms": [{"name": r, "type": "lab" if is_lab(r) else "salla"} for r in rooms],
        "lessons": out,
    }


def run(limit=0):
    lessons = crawl(limit=limit)
    data = build_dataset(lessons)
    if not data["rooms"]:
        raise RuntimeError("No rooms found. The page layout was not recognised - run: python scraper.py --inspect")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT_FILE)
    labs = sum(r["type"] == "lab" for r in data["rooms"])
    print(f"[saved] {OUT_FILE}: {len(data['rooms'])} rooms ({labs} labs), {len(data['lessons'])} lessons")
    return data


# --------------------------------------------------------------------------- inspect
def inspect():
    dbg = HERE / "debug"
    dbg.mkdir(exist_ok=True)
    f = Fetcher()
    for path in START_PATHS:
        url = urljoin(BASE_URL, path)
        print("=" * 70, f"\n{url}")
        r = f.fetch(url)
        if r is None:
            print("  no response (server down, or blocked from this network?)")
            continue
        name = re.sub(r"\W+", "_", path).strip("_") or "index"
        (dbg / f"{name}.html").write_text(r.text, encoding="utf-8")
        soup = BeautifulSoup(r.text, "html.parser")
        print(f"  status {r.status_code}, {len(r.text)} chars, title: {soup.title.get_text(strip=True) if soup.title else '-'}")
        print(f"  saved -> debug/{name}.html")
        for form in soup.find_all("form"):
            print(f"  FORM action={form.get('action')!r} method={form.get('method')!r}")
        for s in soup.find_all("select"):
            o = real_options(s)
            print(f"  SELECT name={s.get('name')!r} id={s.get('id')!r} in_form={s.find_parent('form') is not None} "
                  f"options={len(o)} e.g. {[t for _, t in o[:4]]} onchange={s.get('onchange')!r}")
        for i, t in enumerate(soup.find_all("table")):
            rows = table_rows(t)
            print(f"  TABLE {i}: {len(rows)} rows; first rows:")
            for tr in rows[:3]:
                print("     |", " | ".join(cell_text(c).replace("\n", " / ")[:25] for c in tr.find_all(["td", "th"])))
        for fr in soup.find_all(["iframe", "frame"], src=True):
            print(f"  IFRAME {fr['src']}")
        urls = {m.group(1) for s in soup.find_all("script") for m in AJAX_RE.finditer(s.get_text())}
        if urls:
            print(f"  AJAX URLs in scripts: {sorted(urls)}")
    print("=" * 70, "\nTrying a quick scrape (2 options per dropdown)...")
    data = build_dataset(crawl(limit=2, verbose=False))
    print(f"  rooms found: {[r['name'] for r in data['rooms']][:30]}")
    for l in data["lessons"][:8]:
        print(f"  {DAY_NAMES[l['day']]} {l['start']}-{l['end']} {l['room']:<14} {l['subject'][:30]} | {l['teacher'][:25]}")
    if not data["rooms"]:
        print("\n  Nothing parsed. Send the files in debug/ so the parser can be adjusted.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inspect", action="store_true", help="show page structure and save HTML to debug/")
    ap.add_argument("--limit", type=int, default=0, help="only N options per dropdown (testing)")
    a = ap.parse_args()
    if a.inspect:
        inspect()
    else:
        try:
            run(limit=a.limit)
        except RuntimeError as e:
            print(f"[fail] {e}", file=sys.stderr)
            sys.exit(1)
