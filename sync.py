#!/usr/bin/env python3
"""
sync.py - scrape FBref schedules with soccerdata and load them into the SQL table `fixtures`.

    DATABASE_URL=postgres://... python sync.py                 # all leagues in leagues.json, current season
    python sync.py --dry-run                                   # build rows, write ./out/*.csv, touch no database
    python sync.py --league "ENG-Premier League" --season 2025-2026
    python sync.py --create-table                              # also CREATE TABLE IF NOT EXISTS fixtures

leagues.json (one object per league):
    league        soccerdata league id (see soccerdata docs / custom league_dict.json)
    comp_id       FBref competition number (the 9 in /comps/9/...). Becomes league_id; event_id = comp_id*10000 + n
    country       3-letter prefix used by teams.csv slugs (eng, esp, ...) - narrows team matching
    tz            timezone FBref shows kick-off times in (venue local time) -> converted to UTC
    season_style  "split" (2026-2027, label 2026/27) or "calendar" (2026, label 2026)

Per league and season everything loads in ONE transaction (DELETE that league+season, INSERT fresh rows).
Skipped (nothing deleted) if the scrape fails, 0 matches come back, or played matches would shrink >20% (FORCE=1 overrides).
Teams are matched by name to teams.csv (ESPN ids). If one fails, add {"<name as soccerdata gives it>": <teamId>}
to team_overrides.json.  PROXY=http://... is passed on to soccerdata if FBref blocks your IP.
"""
import argparse, csv, json, os, re, sys, unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FINAL, STATUS_SCHEDULED = "28", "1"      # ESPN-style codes
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
def season_for(style, today=None, override=None):
    """Return (soccerdata season string, label for season_type column)."""
    today = today or datetime.now(timezone.utc)
    if override:
        m = re.fullmatch(r"(\d{4})-(\d{4})", override)
        if m:
            return override, f"{m.group(1)}/{m.group(2)[2:]}"
        return override, override
    if style == "calendar":
        return str(today.year), str(today.year)
    start = today.year if today.month >= 7 else today.year - 1     # European seasons start in summer
    return f"{start}-{start + 1}", f"{start}/{str(start + 1)[2:]}"


# ------------------------------------------------------------------ scrape
def fetch_schedule(league, season):
    import soccerdata as sd
    proxy = os.environ.get("PROXY") or None
    try:
        fb = sd.FBref(leagues=league, seasons=season, proxy=proxy, no_cache=True)
    except TypeError:                                       # newer soccerdata versions may not accept `proxy`
        fb = sd.FBref(leagues=league, seasons=season, no_cache=True)
    return fb.read_schedule().reset_index()


SCORE_RE = re.compile(r"(?:\((\d+)\)\s*)?(\d+)\s*-\s*(\d+)(?:\s*\((\d+)\))?")


def parse_score(raw):
    """'2–1' -> (2,1,None,None);  '(4) 1–1 (5)' -> (1,1,4,5);  NaN/'' -> all None."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return (None,) * 4
    s = str(raw).replace("\u2013", "-").replace("\u2014", "-").strip()
    m = SCORE_RE.fullmatch(s)
    if not m:
        return (None,) * 4
    hso, hs, as_, aso = [int(x) if x else None for x in m.groups()]
    return hs, as_, hso, aso


def parse_attendance(v):
    """73000.0 -> '73000';  '61,000' -> '61000';  NaN/'' -> ''."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (int, float)):
        return str(int(v))
    return re.sub(r"\D", "", str(v))


def to_utc(date, time_, tz):
    """FBref gives a date and a venue-local 'HH:MM'. Returns (utc string, time_tbd)."""
    d = pd.to_datetime(date, errors="coerce")
    if pd.isna(d):
        return None, False
    t = None if time_ is None or (isinstance(time_, float) and pd.isna(time_)) else str(time_).strip()
    if d.hour or d.minute:                                  # date column already carries a time
        naive, tbd = d.to_pydatetime(), False
    elif t and re.match(r"\d{1,2}:\d{2}", t):
        h, m = map(int, t.split(":")[:2])
        naive, tbd = d.to_pydatetime().replace(hour=h, minute=m), False
    else:
        return d.strftime("%Y-%m-%d") + " 00:00:00+00", True   # kick-off not set yet
    utc = naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M:%S+00"), tbd


def clean_schedule(df, tz):
    out = []
    for r in df.to_dict("records"):
        if not isinstance(r.get("home_team"), str) or not isinstance(r.get("away_team"), str):
            continue
        date, tbd = to_utc(r.get("date"), r.get("time"), tz)
        if not date:
            continue
        hs, as_, hso, aso = parse_score(r.get("score"))
        att = parse_attendance(r.get("attendance"))
        gid = r.get("game_id")
        out.append(dict(home=r["home_team"], away=r["away_team"], date=date, time_tbd=tbd, hs=hs, as_=as_, hso=hso,
                        aso=aso, att=att, game_id=gid if isinstance(gid, str) else "",
                        played=hs is not None))
    return out


# ------------------------------------------------------------------ team matching (soccerdata name -> teams.csv row)
STOP = {"fc", "cf", "afc", "ud", "cd", "sd", "ac", "as", "sc", "the", "de", "and", "club", "a"}


def toks(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t not in STOP}


def similarity(name, row):
    cands = [row["displayName"], row["name"], row["shortDisplayName"], row["location"],
             row["slug"].split(".", 1)[-1].replace("_", " ").replace("-", " ")]
    A, best = toks(name), 0.0
    for c in cands:
        B = toks(c)
        if A and B:
            jac = len(A & B) / len(A | B)
            best = max(best, jac if jac < 1 else 1.0 + (0.01 if A == B else 0))
    return best


def is_side_team(row):                         # women's / youth / reserve sides
    s = row["slug"].lower()
    return bool(re.search(r"(\.w$|_w$|\.u\d\d|_u\d\d|\.b$|\.ii$|\.betbrain|women|\.w\.)", s)) or \
        bool(re.search(r"\b(u\d\d|women)\b", row["displayName"].lower()))


def match_teams(names, rows, country, overrides):
    by_id = {r["teamId"]: r for r in rows}
    pool = [r for r in rows if r["slug"].split(".")[0] == country and not is_side_team(r)] or \
           [r for r in rows if not is_side_team(r)]
    result, bad = {}, []
    for n in sorted(names):
        if n in overrides:
            if str(overrides[n]) not in by_id:
                sys.exit(f"team_overrides.json: teamId {overrides[n]} is not in teams.csv")
            result[n] = by_id[str(overrides[n])]
            continue
        ranked = sorted(((similarity(n, r), r) for r in pool), key=lambda x: -x[0])
        top, second = ranked[0], ranked[1]
        if top[0] < 0.5:                       # not in the country's list: accept an exact unique match anywhere
            wide = sorted(((similarity(n, r), r) for r in rows if not is_side_team(r)), key=lambda x: -x[0])
            if wide[0][0] >= 1.0 and wide[1][0] < 1.0:
                result[n] = wide[0][1]
                continue
        if top[0] < 0.5 or (second[0] >= top[0] - 0.02 and second[0] > 0.5):
            alts = ", ".join(f"{r['teamId']}={r['displayName']} [{r['slug']}]" for _, r in ranked[:4])
            bad.append(f"  {n!r}: UNSURE: {alts}")
            continue
        result[n] = top[1]
    if bad:
        sys.exit("Teams not matched - add {\"<name>\": <teamId>} to team_overrides.json and rerun:\n" + "\n".join(bad))
    return result


# ------------------------------------------------------------------ build fixtures rows
def build_rows(matches, cfg, label, team_of, now):
    base = cfg["comp_id"] * 10000
    # stable ids: sorted by FBref's match id, so dates/times moving never renumber anything
    order = sorted(matches, key=lambda m: (m["game_id"] or "~", m["home"], m["away"], m["date"]))
    rows = []
    for i, m in enumerate(order, 1):
        ht, at, eid = team_of[m["home"]], team_of[m["away"]], base + i
        if m["played"]:
            hv, av = (m["hs"], m["hso"] or 0), (m["as_"], m["aso"] or 0)
            hw, aw = str(hv > av).lower(), str(av > hv).lower()
        else:
            hw = aw = None
        vals = [eid, label, cfg["comp_id"], eid, m["date"], int(ht["venueId"] or 0) or None, m["att"] or None,
                ht["teamId"], at["teamId"], hw, aw, m["hs"], m["as_"], m["hso"], m["aso"],
                STATUS_FINAL if m["played"] else STATUS_SCHEDULED, now,
                ht["abbreviation"], ht["displayName"], at["abbreviation"], at["displayName"]]
        rows.append([None if v in (None, "") else str(v) for v in vals])
    return sorted(rows, key=lambda r: int(r[3]))


# ------------------------------------------------------------------ database
def load(conn_url, cfg, label, rows, force, create_table):
    import psycopg
    played = sum(r[COLUMNS.index("status_id")] == STATUS_FINAL for r in rows)
    with psycopg.connect(conn_url) as conn:                  # `with` = one transaction, rolled back on error
        if create_table:
            conn.execute(DDL)
        # 4-letter abbreviations (COMO, ROMA) need the wider columns; harmless to run again
        conn.execute('ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), '
                     'ALTER COLUMN "away_team_abbr" TYPE varchar(10)')
        where, params = '"league_id" = %s AND "season_type" = %s', (str(cfg["comp_id"]), label)
        if not force:
            old = conn.execute(f'SELECT count(*) FROM "fixtures" WHERE {where} AND "status_id" = %s',
                               params + (STATUS_FINAL,)).fetchone()[0]
            if old and played < old * 0.8:
                print(f"  played would drop {old} -> {played}; skipping. Re-run with FORCE=1 if that is right.")
                return
        conn.execute(f'DELETE FROM "fixtures" WHERE {where}', params)
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        with conn.cursor() as cur:
            cur.executemany(f'INSERT INTO "fixtures" ({cols}) VALUES ({", ".join(["%s"] * len(COLUMNS))})', rows)
    print(f"  loaded {played} played + {len(rows) - played} unplayed")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="FBref (soccerdata) -> fixtures table")
    ap.add_argument("--leagues", default=os.path.join(HERE, "leagues.json"))
    ap.add_argument("--teams", default=os.path.join(HERE, "teams.csv"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "team_overrides.json"))
    ap.add_argument("--league", help="only this soccerdata league id, e.g. 'ENG-Premier League'")
    ap.add_argument("--season", help="override the season, e.g. 2025-2026 (split) or 2025 (calendar)")
    ap.add_argument("--dry-run", action="store_true", help="write ./out/*.csv, touch no database")
    ap.add_argument("--create-table", action="store_true", help="CREATE TABLE IF NOT EXISTS fixtures first")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url and not args.dry_run:
        sys.exit("Set DATABASE_URL (your Postgres/Neon connection string) or use --dry-run")

    leagues = json.load(open(args.leagues, encoding="utf-8"))
    if args.league:
        leagues = [l for l in leagues if l["league"] == args.league] or sys.exit(f"{args.league} not in {args.leagues}")
    rows_csv = list(csv.DictReader(open(args.teams, encoding="utf-8-sig")))
    overrides = json.load(open(args.overrides)) if os.path.exists(args.overrides) else {}
    force = os.environ.get("FORCE") == "1"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    failed = []
    for cfg in leagues:
        season, label = season_for(cfg.get("season_style", "split"), override=args.season)
        print(f"\n== {cfg['league']} {label}", flush=True)
        try:
            matches = clean_schedule(fetch_schedule(cfg["league"], season), cfg.get("tz", "UTC"))
            if not matches:
                print("  0 matches returned, skipping (nothing deleted)")
                continue
            names = {m["home"] for m in matches} | {m["away"] for m in matches}
            team_of = match_teams(names, rows_csv, cfg["country"], overrides)
            rows = build_rows(matches, cfg, label, team_of, now)
            tbd = sum(m["time_tbd"] for m in matches)
            print(f"  {sum(m['played'] for m in matches)} played, {sum(not m['played'] for m in matches)} unplayed "
                  f"({tbd} with kick-off time TBD)")
            if args.dry_run:
                os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
                path = os.path.join(HERE, "out", re.sub(r"\W+", "_", cfg["league"]) + ".csv")
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh); w.writerow(COLUMNS); w.writerows(rows)
                print(f"  wrote {path}")
            else:
                load(url, cfg, label, rows, force, args.create_table)
        except (Exception, SystemExit) as e:        # one broken league must not stop the others
            print(f"  FAILED: {e}"); failed.append(cfg["league"])
    if failed:
        sys.exit("\nFailed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
