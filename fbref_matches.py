#!/usr/bin/env python3
"""
fbref_matches.py - get PLAYED and UNPLAYED matches from a saved FBref "Scores & Fixtures" page,
and write each group as its own re-runnable SQL insert file.

    python fbref_matches.py Ligue_1.html --teams teams.csv --out sql
    python fbref_matches.py Ligue_1.html --teams teams.csv --show unplayed      # just print them

Needs fbref.py (your existing script) in the same folder - it reuses its parser and team matcher.

Writes into --out:
    played_inserts.sql     DELETE of this league+season's finished rows, then INSERT of the played matches
    unplayed_inserts.sql   DELETE of this league+season's scheduled rows, then INSERT of the unplayed matches
    played.csv / unplayed.csv

Re-run whenever you save a fresh copy of the page: run played_inserts.sql, then unplayed_inserts.sql.
Matches that have been played since your last run move from the unplayed file to the played file
automatically, and rescheduled dates/times are refreshed.

Differences from fbref.py:
  * event_id / rn are STABLE: start-id + position when sorted by (home squad id, away squad id), not by date,
    so a kick-off time being set or a match moving never renumbers anything.
  * unplayed matches whose kick-off time is still TBD get 00:00:00+00 (same format as every other row)
    and are flagged time_tbd in the CSV.
  * no goal/card events here - keep using fbref.py for fixture_events.
"""
import argparse, csv, json, os, sys
from datetime import datetime, timezone

import fbref
from fbref import STATUS_FINAL, STATUS_SCHEDULED, esc

COLUMNS = ["rn", "season_type", "league_id", "event_id", "date", "venue_id", "attendance", "home_team_id",
           "away_team_id", "home_team_winner", "away_team_winner", "home_team_score", "away_team_score",
           "home_team_shootout_score", "away_team_shootout_score", "status_id", "update_time",
           "home_team_abbr", "home_team_name", "away_team_abbr", "away_team_name"]


# ------------------------------------------------------------------ get played / unplayed
def get_matches(html_path):
    """Return (page_info, played, unplayed). Each match is the dict from fbref.parse_page plus
    `played` (bool) and `time_tbd` (bool); dates are always 'YYYY-MM-DD HH:MM:SS+00'."""
    pg = fbref.parse_page(html_path)
    played, unplayed = [], []
    for f in pg["fixtures"]:
        f["played"] = f["hs"] is not None
        f["time_tbd"] = len(f["date"]) == 10            # date only -> FBref has no kick-off time yet
        if f["time_tbd"]:
            f["date"] += " 00:00:00+00"
        (played if f["played"] else unplayed).append(f)
    key = lambda f: (f["date"], f["home"]["slug"])
    return pg, sorted(played, key=key), sorted(unplayed, key=key)


# ------------------------------------------------------------------ SQL
def fixture_row(pg, f, eid, ht, at, now):
    if f["played"]:
        hv, av = (f["hs"], f["hso"] or 0), (f["as_"], f["aso"] or 0)
        hw, aw = str(hv > av).lower(), str(av > hv).lower()
    else:
        hw = aw = None
    return [eid, pg["season"], pg["league_id"], eid, f["date"], int(ht["venueId"] or 0) or None, f["att"],
            ht["teamId"], at["teamId"], hw, aw, f["hs"], f["as_"], f["hso"], f["aso"],
            STATUS_FINAL if f["played"] else STATUS_SCHEDULED, now,
            ht["abbreviation"], ht["displayName"], at["abbreviation"], at["displayName"]]


def sql_file(pg, label, status, rows, n=100):
    head = [f"-- {label}: {pg['league']} {pg['season']} ({len(rows)} matches)",
            "-- 4-letter abbreviations (COMO, ROMA) need the wider columns; harmless to run again",
            'ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), '
            'ALTER COLUMN "away_team_abbr" TYPE varchar(10);',
            f'DELETE FROM "fixtures" WHERE "league_id" = {esc(pg["league_id"])} '
            f'AND "season_type" = {esc(pg["season"])} AND "status_id" = {esc(status)};']
    cols = ", ".join(f'"{c}"' for c in COLUMNS)
    for i in range(0, len(rows), n):
        vals = ",\n".join("(" + ", ".join(esc(v) for v in r) + ")" for r in rows[i:i + n])
        head.append(f'INSERT INTO "fixtures" ({cols}) VALUES\n{vals};')
    return "\n".join(head) + "\n"


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS + ["time_tbd"])
        w.writerows(r + [t] for r, t in rows)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="FBref fixtures HTML -> played / unplayed SQL inserts")
    ap.add_argument("html", help="saved FBref Scores & Fixtures page")
    ap.add_argument("--teams", default="teams.csv")
    ap.add_argument("--out", default="sql")
    ap.add_argument("--overrides", default="team_overrides.json")
    ap.add_argument("--start-id", type=int, default=0, help="first fixture gets event_id/rn start-id + 1")
    ap.add_argument("--show", choices=["played", "unplayed", "all"], help="print matches instead of writing files")
    args = ap.parse_args()

    pg, played, unplayed = get_matches(args.html)
    print(f"{pg['league']} {pg['season']}: {len(played)} played, {len(unplayed)} unplayed "
          f"({sum(f['time_tbd'] for f in unplayed)} with kick-off time TBD)")

    if args.show:
        for name, group in (("PLAYED", played), ("UNPLAYED", unplayed)):
            if args.show in (name.lower(), "all"):
                print(f"\n== {name} ==")
                for f in group:
                    score = f"{f['hs']}-{f['as_']}" if f["played"] else "v"
                    print(f"{f['date'][:16]}{'*' if f['time_tbd'] else ' '} {f['home']['short']:<18}{score:^7}{f['away']['short']}")
        return

    rows = list(csv.DictReader(open(args.teams, encoding="utf-8-sig")))
    overrides = json.load(open(args.overrides)) if os.path.exists(args.overrides) else {}
    team_of, report = fbref.match_teams([pg], rows, overrides)
    bad = [r for r in report if r[2] is None]
    if bad:
        for lg, name, _, sc, note in bad:
            print(f"  {name}: {note}")
        sys.exit(f"\n{len(bad)} team(s) not matched. Add {{\"<fbref squad id>\": <teamId>}} to {args.overrides} and rerun.")

    # stable ids: independent of date/time
    order = sorted(played + unplayed, key=lambda f: (f["home"]["fb"], f["away"]["fb"]))
    ids = {(f["home"]["fb"], f["away"]["fb"]): args.start_id + i + 1 for i, f in enumerate(order)}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(args.out, exist_ok=True)
    for name, status, group in (("played", STATUS_FINAL, played), ("unplayed", STATUS_SCHEDULED, unplayed)):
        built = [(fixture_row(pg, f, ids[(f["home"]["fb"], f["away"]["fb"])],
                              team_of[f["home"]["fb"]], team_of[f["away"]["fb"]], now), f["time_tbd"])
                 for f in group]
        built.sort(key=lambda x: x[0][3])
        with open(os.path.join(args.out, f"{name}_inserts.sql"), "w", encoding="utf-8") as fh:
            fh.write(sql_file(pg, name, status, [b[0] for b in built]))
        write_csv(os.path.join(args.out, f"{name}.csv"), built)
        print(f"wrote {args.out}/{name}_inserts.sql and {name}.csv ({len(built)} rows)")


if __name__ == "__main__":
    main()
