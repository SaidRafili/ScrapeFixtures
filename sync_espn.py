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
import argparse, calendar, csv, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ESPN_BASE", "https://site.api.espn.com/apis/site/v2/sports/soccer")
FINISHED = ("28", "45", "46", "47")     # same set the Futlog site treats as a result: full time, extra time, penalties
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


EVENT_COLUMNS = ["event_id", "team_side", "minute", "event_type", "player", "assist"]
EVENTS_DDL = """CREATE TABLE IF NOT EXISTS "fixture_events" (
\t"event_id" varchar(255), "team_side" varchar(255), "minute" varchar(255),
\t"event_type" varchar(255), "player" varchar(255), "assist" varchar(255)
);
CREATE INDEX IF NOT EXISTS fixture_events_event_idx ON "fixture_events" ("event_id");"""
RECENT_DAYS = 3        # finished matches this recent are re-fetched every run (VAR / late corrections)


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


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = y + (m == 12), m % 12 + 1


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


def _events(data):
    return data.get("events") or []


def fetch_month(slug, y, m):
    """ESPN no longer accepts date ranges (dates=A-B -> 400), only dates=YYYYMMDD or dates=YYYYMM.
    Ask for the month; if that is refused or may be truncated, ask day by day."""
    url = f"{BASE}/{slug}/scoreboard?dates={y}{m:02d}"
    for extra, cap in (("&limit=1000", 1000), ("", 100)):
        try:
            evs = _events(get_json(url + extra))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise RuntimeError(f"{url}{extra}: {e}")
            continue
        if len(evs) < cap:                                  # got everything
            return evs
        break                                               # looks truncated -> per day
    print(f"  {y}-{m:02d}: month request refused/truncated, fetching day by day", flush=True)
    out = []
    for day in range(1, calendar.monthrange(y, m)[1] + 1):
        try:
            out += _events(get_json(f"{BASE}/{slug}/scoreboard?dates={y}{m:02d}{day:02d}"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{slug} {y}{m:02d}{day:02d}: {e}")
        time.sleep(0.2)
    return out


def fetch_events(slug, start, end):
    events = {}
    for y, m in months(start, end):
        for ev in fetch_month(slug, y, m):
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


# ------------------------------------------------------------------ goals & cards (fixture_events)
def classify(text):
    """ESPN keyEvent type text -> our event_type, or None for events we do not store (subs, VAR, misses...)."""
    t = (text or "").lower()
    if "shootout" in t:
        return None
    if "own" in t and "goal" in t:
        return "own_goal"
    if "penalty" in t:
        return None if any(w in t for w in ("miss", "saved", "save", "hit", "post", "bar")) else "penalty_goal"
    if "goal" in t:
        return "goal"
    if "yellow" in t:
        return "second_yellow" if "second" in t else "yellow_card"
    if "red" in t:
        return "red_card"
    return None


def parse_key_events(summary, event_id, home_id, away_id):
    out = []
    for k in summary.get("keyEvents") or []:
        et = classify((k.get("type") or {}).get("text") or (k.get("type") or {}).get("type"))
        if not et or k.get("shootout"):
            continue
        tid = str((k.get("team") or {}).get("id") or "")
        side = "home" if tid == str(home_id) else "away" if tid == str(away_id) else None
        if not side:
            continue
        minute = str((k.get("clock") or {}).get("displayValue") or "").replace("'", "").replace(" ", "")
        names = [(p.get("athlete") or {}).get("displayName") for p in k.get("participants") or []]
        names = [n for n in names if n]
        player = names[0] if names else None
        assist = names[1] if len(names) > 1 and et in ("goal", "penalty_goal") else None
        out.append([event_id, side, minute or None, et, player, assist])
    return out


def fetch_match_events(slug, rows, skip_ids):
    """rows = fixtures rows; fetch goals/cards for finished matches not in skip_ids."""
    ix = {c: COLUMNS.index(c) for c in ("event_id", "home_team_id", "away_team_id", "status_id")}
    todo = [r for r in rows if r[ix["status_id"]] in FINISHED and r[ix["event_id"]] not in skip_ids]
    out, ids = [], set()
    print(f"  fetching goals/cards for {len(todo)} matches", flush=True)
    for n, r in enumerate(todo, 1):
        eid = r[ix["event_id"]]
        try:
            summary = get_json(f"{BASE}/{slug}/summary?event={eid}")
        except Exception as e:                              # one bad match must not stop the rest
            print(f"  events for {eid} failed: {e}")
            continue
        out += parse_key_events(summary, eid, r[ix["home_team_id"]], r[ix["away_team_id"]])
        ids.add(eid)
        time.sleep(0.2)
    return out, ids


def stored_event_ids(url, rows, create_table):
    """Finished matches that already have events stored, minus recently played ones (those get refreshed)."""
    import psycopg
    cutoff = (datetime.now(timezone.utc).date().toordinal() - RECENT_DAYS)
    old = [r[COLUMNS.index("event_id")] for r in rows
           if r[COLUMNS.index("status_id")] in FINISHED
           and date.fromisoformat(r[COLUMNS.index("date")][:10]).toordinal() < cutoff]
    with psycopg.connect(url) as conn:
        if create_table:
            conn.execute(EVENTS_DDL)
        got = conn.execute('SELECT DISTINCT "event_id" FROM "fixture_events" WHERE "event_id" = ANY(%s)', (old,)).fetchall()
    return {g[0] for g in got}


# ------------------------------------------------------------------ database
def load(url, cfg, label, rows, force, create_table, ev_rows=(), ev_ids=()):
    import psycopg
    played = sum(r[COLUMNS.index("status_id")] in FINISHED for r in rows)
    with psycopg.connect(url) as conn:                      # one transaction, rolled back on error
        if create_table:
            conn.execute(DDL)
            conn.execute(EVENTS_DDL)
        conn.execute('ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), '
                     'ALTER COLUMN "away_team_abbr" TYPE varchar(10)')
        where, params = '"league_id" = %s AND "season_type" = %s', (str(cfg["comp_id"]), label)
        if not force:
            old = conn.execute(f'SELECT count(*) FROM "fixtures" WHERE {where} AND "status_id" = ANY(%s)',
                               params + (list(FINISHED),)).fetchone()[0]
            if old and played < old * 0.8:
                print(f"  finished would drop {old} -> {played}; skipping. Re-run with FORCE=1 if that is right.")
                return
        conn.execute(f'DELETE FROM "fixtures" WHERE {where}', params)
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        with conn.cursor() as cur:
            cur.executemany(f'INSERT INTO "fixtures" ({cols}) VALUES ({", ".join(["%s"] * len(COLUMNS))})', rows)
        if ev_ids:                                          # replace events of the matches we just re-fetched
            conn.execute('DELETE FROM "fixture_events" WHERE "event_id" = ANY(%s)', (list(ev_ids),))
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO "fixture_events" ("event_id","team_side","minute","event_type","player","assist") '
                                'VALUES (%s,%s,%s,%s,%s,%s)', ev_rows)
    print(f"  loaded {played} finished + {len(rows) - played} other, {len(ev_rows)} goal/card events for {len(ev_ids)} matches")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="ESPN API -> fixtures table")
    ap.add_argument("--leagues", default=os.path.join(HERE, "leagues.json"))
    ap.add_argument("--league", help="only this ESPN slug, e.g. eng.1")
    ap.add_argument("--season", help="2025-2026 (split) or 2025 (calendar)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create-table", action="store_true")
    ap.add_argument("--no-events", action="store_true", help="skip goals/cards (fixture_events)")
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
            ev_rows, ev_ids = [], set()
            if not args.no_events:
                skip = set() if args.dry_run else stored_event_ids(url, rows, args.create_table)
                ev_rows, ev_ids = fetch_match_events(cfg["espn"], rows, skip)
            if args.dry_run:
                os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
                path = os.path.join(HERE, "out", re.sub(r"\W+", "_", cfg["espn"]) + ".csv")
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh); w.writerow(COLUMNS); w.writerows(rows)
                print(f"  wrote {path}")
                if ev_rows:
                    with open(os.path.join(HERE, "out", re.sub(r"\W+", "_", cfg["espn"]) + "_events.csv"), "w", newline="", encoding="utf-8") as fh:
                        w = csv.writer(fh); w.writerow(EVENT_COLUMNS); w.writerows(ev_rows)
            else:
                load(url, cfg, label, rows, force, args.create_table, ev_rows, ev_ids)
        except (Exception, SystemExit) as e:
            print(f"  FAILED: {e}"); failed.append(cfg["espn"])
    if failed:
        sys.exit("\nFailed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
