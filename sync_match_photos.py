#!/usr/bin/env python3
"""
sync_match_photos.py - find FREELY LICENSED photos of a specific match on Wikimedia Commons, for match posters.

    python sync_match_photos.py --lookup "Azerbaijan" "Sweden" --date 2025-09-09          # one match, no database
    DATABASE_URL=... python sync_match_photos.py --limit 50 --national                    # finished national-team matches without a photo

What is (and is not) used
  * ONLY Wikimedia Commons, ONLY licences that allow commercial use + adaptation: CC0, Public Domain, CC BY, CC BY-SA (any version).
    CC BY-NC / -ND, "fair use", unknown licences are rejected.
  * NEVER ESPN / Getty / AP / Reuters / club or federation sites. Scraping those and putting the pictures on your own poster is copyright
    infringement, whatever the technical means, and their terms of use forbid it.
  * A photo is accepted only when Commons' own metadata says it was taken on the match date (+/- 1 day) AND the file name / description names one of the teams.
    This avoids putting a photo of a different match on the poster.
  * The credit line (author, licence, link to the file page) is saved next to the URL: CC BY / BY-SA REQUIRE showing it (put it in small print on the poster).
    CC BY-SA also requires that your adapted poster is shared under the same licence - if that is a problem, set --licences cc0,pd,cc-by.

Table `match_photos`: event_id, photourl (Wikimedia thumbnail, 1280 px), credit, licence, filepage, taken_on.
"""
import argparse, csv, json, os, re, sys, time
from datetime import date, datetime, timedelta

import sync_venue_photos as svp

ALLOWED = {
    "cc0":    re.compile(r"^(cc0|cc[- ]?zero|public domain|pd\b|pdm)", re.I),
    "pd":     re.compile(r"public domain|^pd\b|^pd[- ]", re.I),
    "cc-by":  re.compile(r"^cc[- ]by[- ]\d", re.I),                  # 'CC BY 4.0' but not 'CC BY-SA', 'CC BY-NC'
    "cc-by-sa": re.compile(r"^cc[- ]by[- ]sa[- ]\d", re.I),
}
BLOCK = re.compile(r"\b(nc|nd)\b|non-?commercial|no[- ]deriv|fair use|all rights reserved", re.I)
DDL = """CREATE TABLE IF NOT EXISTS "match_photos" (
  "event_id" varchar(255) PRIMARY KEY, "photourl" varchar(500) NOT NULL, "credit" varchar(255) NOT NULL DEFAULT '',
  "licence" varchar(60) NOT NULL DEFAULT '', "filepage" varchar(500) NOT NULL DEFAULT '', "taken_on" varchar(20) NOT NULL DEFAULT ''
);"""


# ------------------------------------------------------------------ pure helpers
def licence_ok(short_name, allowed=("cc0", "pd", "cc-by", "cc-by-sa")):
    s = (short_name or "").strip()
    if not s or BLOCK.search(s):
        return False
    return any(ALLOWED[k].search(s) for k in allowed)


def parse_taken(meta):
    """Commons extmetadata DateTimeOriginal ('2025-09-09 20:41:03' or '<time>2025-09-09</time>') -> date or None."""
    raw = svp.strip_html((meta.get("DateTimeOriginal") or {}).get("value"))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def names_team(text, team_names):
    hay = set(svp.tokens(text))
    for t in team_names:
        want = svp.distinctive(t) or svp.tokens(t)
        if want and all(w in hay for w in want):
            return True
    return False


def pick(files_meta, match_day, team_names, allowed):
    """files_meta: list of dicts {file, url, credit, licence, taken, text, width}. -> best acceptable dict or None."""
    ok = []
    for f in files_meta:
        if not licence_ok(f["licence"], allowed):
            continue
        if not f["taken"] or abs((f["taken"] - match_day).days) > 1:
            continue
        if not names_team(f["file"].replace("_", " ") + " " + f["text"], team_names):
            continue
        ok.append(f)
    return max(ok, key=lambda f: f["width"], default=None)


# ------------------------------------------------------------------ Commons
def file_info(filename, width):
    j = svp.api("commons.wikimedia.org", action="query", titles="File:" + filename, prop="imageinfo",
                iiprop="url|size|mime|extmetadata", iiurlwidth=width)
    page = next(iter((j.get("query") or {}).get("pages", [])), None)
    if not page or page.get("missing") or not page.get("imageinfo"):
        return None
    ii = page["imageinfo"][0]
    meta = ii.get("extmetadata") or {}
    if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp") or int(ii.get("width") or 0) < 800:
        return None
    return {"file": filename, "url": ii.get("thumburl") or ii.get("url"), "credit": svp.credit_line(meta),
            "licence": svp.strip_html((meta.get("LicenseShortName") or {}).get("value")),
            "taken": parse_taken(meta), "width": int(ii.get("width") or 0),
            "text": svp.strip_html((meta.get("ImageDescription") or {}).get("value")),
            "page": ii.get("descriptionurl", "")}


def find_match_photo(home, away, match_day, width=1280, allowed=("cc0", "pd", "cc-by", "cc-by-sa")):
    """-> {url, credit, licence, page, taken_on} or {miss: reason}"""
    seen, cands = set(), []
    for q in (f"{home} {away} {match_day.year}", f"{home} {away}", f"{home} national football team {match_day.year}"):
        try:
            names = svp.api("commons.wikimedia.org", action="query", list="search", srsearch=q, srnamespace=6, srlimit=15)
        except Exception as e:                              # noqa: BLE001
            return {"miss": f"search error: {e}"}
        for s in (names.get("query") or {}).get("search", []):
            fn = s["title"].split(":", 1)[1].replace(" ", "_")
            if fn not in seen and svp.is_photo_file(fn):
                seen.add(fn); cands.append(fn)
        time.sleep(0.3)
    metas = []
    for fn in cands[:25]:
        try:
            m = file_info(fn, width)
        except Exception:                                   # noqa: BLE001
            m = None
        if m:
            metas.append(m)
    best = pick(metas, match_day, [home, away], allowed)
    if not best:
        return {"miss": f"{len(metas)} candidate file(s), none free + taken on {match_day} + naming a team"}
    return {"url": best["url"], "credit": best["credit"], "licence": best["licence"], "page": best["page"], "taken_on": str(best["taken"])}


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Free Commons photos of a match -> match_photos")
    ap.add_argument("--lookup", nargs=2, metavar=("HOME", "AWAY"))
    ap.add_argument("--date", help="with --lookup: match date YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--national", action="store_true", help="only national-team competitions (league ids 800+)")
    ap.add_argument("--licences", default="cc0,pd,cc-by,cc-by-sa", help="allowed licence families, comma separated")
    ap.add_argument("--width", type=int, default=1280, choices=svp.STEPS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    allowed = tuple(x.strip() for x in a.licences.split(",") if x.strip())

    if a.lookup:
        if not a.date:
            sys.exit("--lookup needs --date YYYY-MM-DD")
        print(json.dumps(find_match_photo(a.lookup[0], a.lookup[1], date.fromisoformat(a.date), a.width, allowed), indent=2, ensure_ascii=False))
        return

    url = os.environ.get("DATABASE_URL") or sys.exit("Set DATABASE_URL (or use --lookup)")
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(DDL)
        rows = conn.execute("""SELECT f.event_id, f.date, f.home_team_name, f.away_team_name FROM fixtures f
                               LEFT JOIN match_photos p ON p.event_id = f.event_id
                               WHERE p.event_id IS NULL AND f.status_id IN ('28','45','46','47') AND f.home_team_name IS NOT NULL
                                 AND (%(nat)s = false OR (CASE WHEN f.league_id ~ '^[0-9]+$' THEN f.league_id::int END) >= 800)
                               ORDER BY f.date DESC LIMIT NULLIF(%(lim)s, 0)""", {"nat": a.national, "lim": a.limit}).fetchall()
        print(f"{len(rows)} match(es) to look up")
        out = []
        for eid, d, h, w in rows:
            day = datetime.fromisoformat(d.replace("Z", "+00:00")[:19]).date() if isinstance(d, str) else d.date()
            r = find_match_photo(h, w, day, a.width, allowed)
            if r.get("url"):
                print(f"saved  {h} v {w} {day}: {r['page']} ({r['licence']})")
                out.append([eid, r["url"], r["credit"], r["licence"], r["page"], r["taken_on"]])
                if not a.dry_run:
                    conn.execute("""INSERT INTO match_photos VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING""", out[-1])
            else:
                print(f"none   {h} v {w} {day}: {r['miss']}")
            time.sleep(0.4)
        if a.dry_run and out:
            os.makedirs("out", exist_ok=True)
            with open("out/match_photos.csv", "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh, quoting=csv.QUOTE_ALL).writerows([["event_id", "photourl", "credit", "licence", "filepage", "taken_on"]] + out)


if __name__ == "__main__":
    main()
