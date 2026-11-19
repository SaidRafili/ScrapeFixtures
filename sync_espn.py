#!/usr/bin/env python3
"""
sync_espn.py - load league fixtures from ESPN's public JSON API into the SQL table `fixtures`.

No browser, no captcha, no HTML parsing, no team-name matching: ESPN's team ids ARE the ids in teams.csv.

    DATABASE_URL=postgres://... python sync_espn.py              # all leagues in leagues.json, current season
    python sync_espn.py --dry-run                                # write ./out/<league>.csv, touch no database
    python sync_espn.py --league eng.1 --season 2025-2026
    python sync_espn.py --create-table

leagues.json (one object per league):  {"espn": "eng.1", "comp_id": 9, "season_style": "split"}
    espn          ESPN league slug (eng.1, esp.1, ger.1, ita.1, fra.1 ...)
    comp_id       written to league_id (kept equal to the old FBref number so existing rows are replaced)
    season_style  "split" (Jul-Jun, label 2026/27) or "calendar" (Jan-Dec, label 2026)

Per league+season: DELETE + INSERT in ONE transaction. Skipped (nothing deleted) if the fetch fails, returns
0 matches, or finished matches would shrink >20% vs the table (FORCE=1 overrides).
"""
import argparse, csv, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ESPN_BASE", "https://site.api.espn.com/apis/site/v2/sports/soccer")
FINAL = "28"
COLUMNS = ["rn", "season_type", "league_id", "event_id", "date", "venue_id", "attendance", "home_team_id",
           "away_team_id", "home_team_winner", "away_team_winner", "home_team_score", "away_team_score",
           "home_team_shootout_score", "away_team_shootout_score", "status_id", "update_time",
           "home_team_abbr", "home_team_name", "away_team_abbr", "away_team_name"]
DDL = """CREATE TABLE IF NOT EXISTS "fixtures" (
\t"rn" varchar(255), "season_type" varchar(255), "league_id" varchar(255), "event_id" varchar(255),
\t"date" varchar(255), "venue_id" varchar(255), "attendance" varchar(255), "home_team_id" varchar(255),
\t"away_team_id" varchar(255), "home_team_winner" varchar(255), "away_team_winner" varchar(255),
\t"home_team_score" varchar(255), "away_team_score" varchar(255), "home_team_shootout_score" varchar(255),
\t"away_team_shootout_score" varchar(255), "status_id" varchar(255), "update_time" varchar(255),
\t"home_team_abbr" varchar(10), "home_team_name" varchar(100), "away_team_abbr" varchar(10),
\t"away_team_name" varchar(100)
);"""


# ------------------------------------------------------------------ seasons
def season_bounds(style, today=None, override=None):
    """-> (start date, end date, label). split: Jul 1 - Jun 30; calendar: Jan 1 - Dec 31."""
    today = today or datetime.now(timezone.utc).date()
    if style == "calendar":
        y = int(override) if override else today.year
        return date(y, 1, 1), date(y, 12, 31), str(y)
    if override:
        m = re.fullmatch(r"(\d{4})-(\d{4})", override) or sys.exit("--season must look like 2025-2026")
        y = int(m.group(1))
    else:
        y = today.year if today.month >= 7 else today.year - 1
    return date(y, 7, 1), date(y + 1, 6, 30), f"{y}/{str(y + 1)[2:]}"


def date_chunks(start, end, days=10):
    d = start
    while d <= end:
        e = min(date.fromordinal(d.toordinal() + days - 1), end)
        yield d, e
        d = date.fromordinal(e.toordinal() + 1)


# ------------------------------------------------------------------ fetch
def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fixtures-sync)", "Accept": "application/json"})
    for i in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:              # 4xx will not fix itself: fail fast
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        if i == tries:
            raise RuntimeError(f"{url}: {err}")
        time.sleep(3 * i)


def fetch_events(slug, start, end):
    events, use_limit = {}, True
    for a, b in date_chunks(start, end):
        url = f"{BASE}/{slug}/scoreboard?dates={a:%Y%m%d}-{b:%Y%m%d}"
        try:
            data = get_json(url + ("&limit=300" if use_limit else ""))
        except urllib.error.HTTPError as e:
            if e.code != 400 or not use_limit:
                raise RuntimeError(f"{url}: {e}")
            use_limit = False                               # ESPN rejected `limit`: go without it from now on
            print("  ESPN rejected 'limit', retrying without it", flush=True)
            data = get_json(url)
        for ev in data.get("events") or []:
            events[str(ev["id"])] = ev
        time.sleep(0.3)                                     # be polite
    return list(events.values())


# ------------------------------------------------------------------ parse
def num(v):
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return None


def parse_event(ev, cfg, label, now):
    """One ESPN event -> list of column values, or None if it is not a normal two-team match."""
    comp = (ev.get("competitions") or [{}])[0]
    side = {c.get("homeAway"): c for c in comp.get("competitors") or []}
    if "home" not in side or "away" not in side:
        return None
    h, a = side["home"], side["away"]
    ht, at = h.get("team") or {}, a.get("team") or {}
    if not ht.get("id") or not at.get("id"):
        return None
    st = (ev.get("status") or {}).get("type") or {}
    started = st.get("state") in ("in", "post")
    d = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
    hs, as_ = (num(h.get("score")), num(a.get("score"))) if started else (None, None)
    hw = aw = None
    if st.get("completed"):
        hw, aw = str(bool(h.get("winner"))).lower(), str(bool(a.get("winner"))).lower()
    ven = comp.get("venue") or {}
    return [None, label, cfg["comp_id"], ev["id"], d.strftime("%Y-%m-%d %H:%M:%S+00"), ven.get("id"),
            num(comp.get("attendance")) if started and comp.get("attendance") else None,
            ht["id"], at["id"], hw, aw, hs, as_, num(h.get("shootoutScore")), num(a.get("shootoutScore")),
            st.get("id") or "1", now, ht.get("abbreviation"), ht.get("displayName") or ht.get("name"),
            at.get("abbreviation"), at.get("displayName") or at.get("name")]


def build_rows(events, cfg, label, now):
    rows = [r for r in (parse_event(e, cfg, label, now) for e in events) if r]
    rows.sort(key=lambda r: (int(r[3]) if str(r[3]).isdigit() else 0, r[3]))
    base = int(cfg["comp_id"]) * 10000
    for i, r in enumerate(rows, 1):                         # rn: stable position by ESPN event id
        r[0] = base + i
    return [[None if v in (None, "") else str(v) for v in r] for r in rows]


# ------------------------------------------------------------------ database
def load(url, cfg, label, rows, force, create_table):
    import psycopg
    played = sum(r[COLUMNS.index("status_id")] == FINAL for r in rows)
    with psycopg.connect(url) as conn:                      # one transaction, rolled back on error
        if create_table:
            conn.execute(DDL)
        conn.execute('ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), '
                     'ALTER COLUMN "away_team_abbr" TYPE varchar(10)')
        where, params = '"league_id" = %s AND "season_type" = %s', (str(cfg["comp_id"]), label)
        if not force:
            old = conn.execute(f'SELECT count(*) FROM "fixtures" WHERE {where} AND "status_id" = %s',
                               params + (FINAL,)).fetchone()[0]
            if old and played < old * 0.8:
                print(f"  finished would drop {old} -> {played}; skipping. Re-run with FORCE=1 if that is right.")
                return
        conn.execute(f'DELETE FROM "fixtures" WHERE {where}', params)
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        with conn.cursor() as cur:
            cur.executemany(f'INSERT INTO "fixtures" ({cols}) VALUES ({", ".join(["%s"] * len(COLUMNS))})', rows)
    print(f"  loaded {played} finished + {len(rows) - played} other")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="ESPN API -> fixtures table")
    ap.add_argument("--leagues", default=os.path.join(HERE, "leagues.json"))
    ap.add_argument("--league", help="only this ESPN slug, e.g. eng.1")
    ap.add_argument("--season", help="2025-2026 (split) or 2025 (calendar)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create-table", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url and not args.dry_run:
        sys.exit("Set DATABASE_URL or use --dry-run")
    leagues = json.load(open(args.leagues, encoding="utf-8"))
    if args.league:
        leagues = [l for l in leagues if l["espn"] == args.league] or sys.exit(f"{args.league} not in leagues.json")
    force = os.environ.get("FORCE") == "1"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    failed = []
    for cfg in leagues:
        start, end, label = season_bounds(cfg.get("season_style", "split"), override=args.season)
        print(f"\n== {cfg['espn']} {label}", flush=True)
        try:
            rows = build_rows(fetch_events(cfg["espn"], start, end), cfg, label, now)
            if not rows:
                print("  0 matches returned, skipping (nothing deleted)")
                continue
            print(f"  {len(rows)} matches")
            if args.dry_run:
                os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
                path = os.path.join(HERE, "out", re.sub(r"\W+", "_", cfg["espn"]) + ".csv")
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh); w.writerow(COLUMNS); w.writerows(rows)
                print(f"  wrote {path}")
            else:
                load(url, cfg, label, rows, force, args.create_table)
        except (Exception, SystemExit) as e:
            print(f"  FAILED: {e}"); failed.append(cfg["espn"])
    if failed:
        sys.exit("\nFailed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
