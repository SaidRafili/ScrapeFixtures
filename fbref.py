#!/usr/bin/env python3
"""
fbref_to_sql.py - turn saved FBref "Scores & Fixtures" pages into SQL.

    python fbref_to_sql.py Premier_League.html La_Liga.html --teams teams.csv --out sql

Works for any league page of the same kind (Premier League, La Liga, Serie A, Bundesliga, ...).
Several files in one run are fine; every fixture gets a unique event_id.

Writes into --out (default ./sql):
    teams.sql          your whole teams.csv in the `teams` structure
    leagues_venues.sql `leagues` (name, season label, logo) and `venues` (stadium names)
    fixtures.sql       `fixtures` (played + not played), `fixture_events`, view `fixtures_full`
    team_report.txt    which teams.csv row each FBref team was matched to (CHECK IT)
    missing_matches.txt  played matches whose goal events still need their match page

Load order:  teams.sql -> leagues_venues.sql -> fixtures.sql

Teams are matched to teams.csv automatically (same country prefix, similar name). If one is wrong or
unmatched, put the right teamId in team_overrides.json:   {"<fbref squad id>": <teamId>}
Optional goal/card events: save each match page as <pages>/<fbref match id>.html (see missing_matches.txt),
or add --fetch to open them with Playwright (pip install playwright; playwright install chromium).

pip install beautifulsoup4
"""
import argparse, csv, hashlib, json, os, re, sys, time, unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from bs4 import BeautifulSoup

BASE = "https://fbref.com"
STATUS_FINAL, STATUS_SCHEDULED = 28, 1        # ESPN-style codes; change if your app uses others


# ------------------------------------------------------------------ helpers
def esc(v):
    return "NULL" if v in (None, "") else "'" + str(v).replace("'", "''") + "'"


def q(v):                                      # NOT NULL text columns: '' instead of NULL
    return "'" + str(v or "").replace("'", "''") + "'"


def read(path):
    return BeautifulSoup(open(path, encoding="utf-8", errors="ignore"), "html.parser")


def cell(tr, stat):
    td = tr.find(["td", "th"], {"data-stat": stat})
    return td.get_text(strip=True) if td else ""


def strip_tags(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(h or ""))).strip()


def season_label(raw):                         # "2026-2027" -> "2026/27", "2026" -> "2026"
    m = re.fullmatch(r"(\d{4})-(\d{4})", raw or "")
    return f"{m.group(1)}/{m.group(2)[2:]}" if m else (raw or "")


# ------------------------------------------------------------------ parse one FBref page
def parse_page(path):
    soup = read(path)
    table = soup.select_one("table[id^=sched_]")
    if not table:
        sys.exit(f"{path}: no fixtures table (id starts with 'sched_') found - is this a Scores & Fixtures page?")
    m = re.match(r"sched_([\d-]+)_(\d+)_", table["id"])
    season_raw, league_id = m.group(1), int(m.group(2))
    h1 = soup.select_one("h1").get_text(" ", strip=True)
    league_name = re.sub(r"^[\d-]{4,9}\s+|\s+Scores\s*&\s*Fixtures$", "", h1).strip()
    logo = soup.select_one("img.teamlogo")
    cm = soup.select_one('#meta a[href*="/country/"]') or soup.select_one('a[href*="/en/country/"]')
    country = re.search(r"/country/([A-Z]{3})/", cm["href"]).group(1).lower() if cm else ""

    fixtures = []
    for tr in table.select("tbody tr"):
        if not cell(tr, "home_team"):
            continue
        teams = []
        for stat in ("home_team", "away_team"):
            a = tr.select_one(f"td[data-stat={stat}] a")
            mm = re.search(r"/squads/([0-9a-f]{8})/(.+?)-Stats", a["href"]) if a else None
            if not mm:
                sys.exit(f"{path}: team link missing in a fixture row")
            teams.append(dict(fb=mm.group(1), slug=mm.group(2), short=cell(tr, stat)))
        a = tr.select_one("td[data-stat=match_report] a")
        href = a["href"] if a else ""
        mm = re.search(r"/matches/([0-9a-f]{8})/", href)

        score = cell(tr, "score").replace("\u2013", "-").replace("\u2014", "-")
        sm = re.fullmatch(r"(?:\((\d+)\)\s*)?(\d+)\s*-\s*(\d+)(?:\s*\((\d+)\))?", score)
        hs = as_ = hso = aso = None
        if sm:
            hso, hs, as_, aso = [int(x) if x else None for x in sm.groups()]

        date, time_ = cell(tr, "date"), ""
        sp = tr.select_one("span.venuetime")
        if sp and sp.get("data-venue-epoch"):        # exact UTC from FBref's own timestamp
            date, time_ = datetime.fromtimestamp(int(sp["data-venue-epoch"]), timezone.utc) \
                .strftime("%Y-%m-%d %H:%M:%S+00").split(" ")
        fixtures.append(dict(
            date=f"{date} {time_}".strip(), home=teams[0], away=teams[1], hs=hs, as_=as_, hso=hso, aso=aso,
            match_id=mm.group(1) if mm else None, url=BASE + href if mm else None,
            att=re.sub(r"\D", "", cell(tr, "attendance")), venue=cell(tr, "venue")))
    return dict(path=path, league_id=league_id, league=league_name, logo=logo["src"] if logo else "",
                season=season_label(season_raw), country=country, fixtures=fixtures)


# ------------------------------------------------------------------ match FBref teams -> teams.csv rows
STOP = {"fc", "cf", "afc", "ud", "cd", "sd", "ac", "as", "sc", "the", "de", "and", "club", "a"}


def toks(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", s).split() if t not in STOP}


def similarity(fb_names, row):
    cands = [row["displayName"], row["name"], row["shortDisplayName"], row["location"],
             row["slug"].split(".", 1)[-1].replace("_", " ").replace("-", " ")]
    best = 0.0
    for a in fb_names:
        A = toks(a)
        for c in cands:
            B = toks(c)
            if not A or not B:
                continue
            jac = len(A & B) / len(A | B)
            best = max(best, jac if jac < 1 else 1.0 + (0.01 if A == B else 0))
    return best


def is_side_team(row):                         # women's / youth / national / reserve sides
    s = row["slug"].lower()
    return bool(re.search(r"(\.w$|_w$|\.u\d\d|_u\d\d|\.b$|\.ii$|\.betbrain|women|\.w\.)", s)) or \
        bool(re.search(r"\b(u\d\d|women)\b", row["displayName"].lower()))


def match_teams(pages, rows, overrides):
    by_id = {r["teamId"]: r for r in rows}
    result, report = {}, []
    for pg in pages:
        pool = [r for r in rows if r["slug"].split(".")[0] == pg["country"] and not is_side_team(r)] or \
               [r for r in rows if not is_side_team(r)]
        seen = {}
        for f in pg["fixtures"]:
            for t in (f["home"], f["away"]):
                seen[t["fb"]] = t
        for fb, t in seen.items():
            names = [t["slug"].replace("-", " "), t["short"]]
            if str(fb) in overrides:
                row = by_id.get(str(overrides[str(fb)]))
                if not row:
                    sys.exit(f"team_overrides.json: teamId {overrides[str(fb)]} is not in teams.csv")
                result[fb] = row; report.append((pg["league"], names[0], row, 9.9, "override")); continue
            ranked = sorted(((similarity(names, r), r) for r in pool), key=lambda x: -x[0])
            top, second = ranked[0], ranked[1]
            if top[0] < 0.5:        # not in the country's teams: accept an exact, unique name match anywhere in the CSV
                wide = sorted(((similarity(names, r), r) for r in rows if not is_side_team(r)), key=lambda x: -x[0])
                if wide[0][0] >= 1.0 and wide[1][0] < 1.0:
                    result[fb] = wide[0][1]
                    report.append((pg["league"], names[0], wide[0][1], wide[0][0], "found outside the country list"))
                    continue
            if top[0] < 0.5 or (second[0] >= top[0] - 0.02 and second[0] > 0.5):
                alts = ", ".join(f"{r['teamId']}={r['displayName']} [{r['slug']}]" for s, r in ranked[:4])
                report.append((pg["league"], names[0], None, top[0], f"UNSURE: {alts}"))
                continue
            result[fb] = top[1]
            report.append((pg["league"], names[0], top[1], top[0], ""))
    return result, report


# ------------------------------------------------------------------ goal / card events from a saved match page
def parse_events(soup):
    wrap = soup.select_one("#events_wrap")
    out = []
    for ev in (wrap.select("div.event") if wrap else []):
        cls = ev.get("class", [])
        side = "home" if "a" in cls else "away" if "b" in cls else None
        icon = ev.select_one("div.event_icon")
        ic = " ".join(icon.get("class", [])) if icon else ""
        if "goal" in ic:
            typ = "own_goal" if "own" in ic else "penalty_goal" if "pen" in ic else "goal"
        elif "red" in ic:
            typ = "red_card"
        elif "yellow" in ic:
            typ = "yellow_card"
        elif "substitute" in ic:
            typ = "substitution"
        else:
            typ = ic.replace("event_icon", "").strip() or "other"
        txt = ev.get_text("\n", strip=True)
        minute = re.search(r"(\d+(?:\+\d+)?)", txt)
        player = ev.find("a")
        assist = re.search(r"Assist:\s*(.+)", txt)
        out.append((side, minute.group(1) if minute else None, typ,
                    player.get_text(strip=True) if player else None, assist.group(1).strip() if assist else None))
    return out


# ------------------------------------------------------------------ SQL text
TEAMS_DDL = '''CREATE TABLE "teams" (
	"teamid" integer PRIMARY KEY,
	"location" varchar(100) NOT NULL,
	"name" varchar(100) NOT NULL,
	"abbreviation" varchar(30) NOT NULL,
	"displayname" varchar(100) NOT NULL,
	"shortdisplayname" varchar(50) NOT NULL,
	"color" varchar(10) NOT NULL,
	"alternatecolor" varchar(10) NOT NULL,
	"logourl" varchar(255) NOT NULL,
	"venueid" integer NOT NULL,
	"slug" varchar(100) NOT NULL
);
CREATE UNIQUE INDEX "teams_pkey" ON "teams" ("teamid");
'''

LOOKUP_DDL = '''CREATE TABLE "leagues" (
	"leagueid" integer PRIMARY KEY,
	"name" varchar(100) NOT NULL,
	"shortname" varchar(50) NOT NULL,
	"season" varchar(20) NOT NULL,
	"logourl" varchar(255) NOT NULL,
	"slug" varchar(100) NOT NULL
);

CREATE TABLE "venues" (
	"venueid" integer PRIMARY KEY,
	"fullname" varchar(150) NOT NULL,
	"photourl" varchar(500) NOT NULL DEFAULT '',
	"photocredit" varchar(255) NOT NULL DEFAULT ''
);
'''

FIXTURES_DDL = '''CREATE TABLE "fixtures" (
	"rn" varchar(255),
	"season_type" varchar(255),
	"league_id" varchar(255),
	"event_id" varchar(255),
	"date" varchar(255),
	"venue_id" varchar(255),
	"attendance" varchar(255),
	"home_team_id" varchar(255),
	"away_team_id" varchar(255),
	"home_team_winner" varchar(255),
	"away_team_winner" varchar(255),
	"home_team_score" varchar(255),
	"away_team_score" varchar(255),
	"home_team_shootout_score" varchar(255),
	"away_team_shootout_score" varchar(255),
	"status_id" varchar(255),
	"update_time" varchar(255),
	"home_team_abbr" varchar(3),
	"home_team_name" varchar(100),
	"away_team_abbr" varchar(3),
	"away_team_name" varchar(100)
);

CREATE TABLE "fixture_events" (
	"event_id" varchar(255),
	"team_side" varchar(255),
	"minute" varchar(255),
	"event_type" varchar(255),
	"player" varchar(255),
	"assist" varchar(255)
);
'''

VIEW = '''
CREATE VIEW "fixtures_full" AS
SELECT f.*,
       l.name AS league_name, l.logourl AS league_logo, f.season_type AS season_name,
       v.fullname AS venue_name,
       ht.displayname AS home_name, ht.shortdisplayname AS home_short, ht.abbreviation AS home_abbr,
       ht.color AS home_color, ht.alternatecolor AS home_alt_color, ht.logourl AS home_logo,
       at.displayname AS away_name, at.shortdisplayname AS away_short, at.abbreviation AS away_abbr,
       at.color AS away_color, at.alternatecolor AS away_alt_color, at.logourl AS away_logo
  FROM "fixtures" f
  LEFT JOIN "leagues" l ON l.leagueid = f.league_id::int
  LEFT JOIN "venues" v ON v.venueid = f.venue_id::int
  JOIN "teams" ht ON ht.teamid = f.home_team_id::int AND ht.abbreviation = f.home_team_abbr AND ht.displayname = f.home_team_name
  JOIN "teams" at ON at.teamid = f.away_team_id::int AND at.abbreviation = f.away_team_abbr AND at.displayname = f.away_team_name;
'''


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="FBref fixtures HTML -> SQL")
    ap.add_argument("html", nargs="+", help="saved FBref Scores & Fixtures page(s)")
    ap.add_argument("--teams", default="teams.csv")
    ap.add_argument("--out", default="sql")
    ap.add_argument("--pages", default="match_pages", help="folder with saved match pages (<match id>.html)")
    ap.add_argument("--overrides", default="team_overrides.json")
    ap.add_argument("--start-id", type=int, default=0, help="event_id/rn of the first fixture is start-id + 1")
    ap.add_argument("--inserts-only", action="store_true",
                    help="write ONE paste-ready inserts.sql (no CREATE TABLE, teams or view; re-runnable)")
    ap.add_argument("--fetch", action="store_true", help="open missing match pages with Playwright")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    out = lambda n: os.path.join(args.out, n)

    rows = list(csv.DictReader(open(args.teams, encoding="utf-8-sig")))
    overrides = json.load(open(args.overrides)) if os.path.exists(args.overrides) else {}
    pages = [parse_page(p) for p in args.html]

    # ---- teams
    team_of, report = match_teams(pages, rows, overrides)
    lines = [f"{'league':<16}{'FBref team':<28}{'teamId':<8}{'abbr':<6}{'teams.csv displayName':<28}{'score':<6}note"]
    for lg, name, row, sc, note in report:
        lines.append(f"{lg:<16}{name:<28}{(row['teamId'] if row else '-'):<8}{(row['abbreviation'] if row else '-'):<6}"
                     f"{(row['displayName'] if row else '-'):<28}{sc:<6.2f}{note}")
    open(out("team_report.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    bad = [r for r in report if r[2] is None]
    if bad:
        print("\n".join(lines))
        sys.exit(f"\n{len(bad)} team(s) could not be matched safely. Add them to {args.overrides} as "
                 '{"<fbref squad id>": <teamId>} (ids are in the UNSURE list above) and run again.')

    tsql = [TEAMS_DDL]
    for i in range(0, len(rows), 500):
        vals = ["(" + ", ".join([r["teamId"], q(r["location"]), q(r["name"]), q(r["abbreviation"]), q(r["displayName"]),
                                 q(r["shortDisplayName"]), q(r["color"]), q(r["alternateColor"]), q(r["logoURL"]),
                                 r["venueId"] or "0", q(r["slug"])]) + ")" for r in rows[i:i + 500]]
        tsql.append('INSERT INTO "teams" VALUES\n' + ",\n".join(vals) + ";")
    open(out("teams.sql"), "w", encoding="utf-8").write("\n".join(tsql) + "\n")

    # ---- leagues + venues
    leagues, venue_names = {}, {}
    for pg in pages:
        if pg["league_id"] in leagues:
            print(f"note: league {pg['league_id']} appears twice - keeping {pg['season']}")
        leagues[pg["league_id"]] = pg
        for f in pg["fixtures"]:
            vid = int(team_of[f["home"]["fb"]]["venueId"] or 0)
            if vid and f["venue"]:
                venue_names.setdefault(vid, Counter())[f["venue"]] += 1
    lk = [LOOKUP_DDL]
    for lid, pg in sorted(leagues.items()):
        lk.append('INSERT INTO "leagues" VALUES (%d, %s, %s, %s, %s, %s);' % (
            lid, esc(pg["league"]), esc(pg["league"]), esc(pg["season"]), esc(pg["logo"]),
            esc(re.sub(r"[^a-z0-9]+", "-", pg["league"].lower()).strip("-"))))
    for vid, c in sorted(venue_names.items()):
        lk.append('INSERT INTO "venues" ("venueid", "fullname") VALUES (%d, %s);' % (vid, esc(c.most_common(1)[0][0])))
        if len(c) > 1:
            print(f"note: venue {vid} has several names {dict(c)} - using the most common")
    open(out("leagues_venues.sql"), "w", encoding="utf-8").write("\n".join(lk) + "\n")

    # ---- fixtures (+ events)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql, missing, eid, played_n = [FIXTURES_DDL], [], args.start_id, 0
    fx_rows = []
    page = None
    if args.fetch:
        from playwright.sync_api import sync_playwright
        page = sync_playwright().start().chromium.launch(headless=False).new_context().new_page()
    os.makedirs(args.pages, exist_ok=True)

    for pg in pages:
        for f in sorted(pg["fixtures"], key=lambda x: (x["date"], x["home"]["slug"])):
            eid += 1
            ht, at = team_of[f["home"]["fb"]], team_of[f["away"]["fb"]]
            played = f["hs"] is not None
            hw = aw = None
            if played:
                played_n += 1
                hv = (f["hs"], f["hso"] or 0); av = (f["as_"], f["aso"] or 0)
                hw, aw = str(hv > av).lower(), str(av > hv).lower()
            vals = [eid, pg["season"], pg["league_id"], eid, f["date"], int(ht["venueId"] or 0) or None, f["att"],
                    ht["teamId"], at["teamId"], hw, aw, f["hs"], f["as_"], f["hso"], f["aso"],
                    STATUS_FINAL if played else STATUS_SCHEDULED, now,
                    ht["abbreviation"], ht["displayName"], at["abbreviation"], at["displayName"]]
            fx_rows.append("(" + ", ".join(esc(v) for v in vals) + ")")
            sql.append('INSERT INTO "fixtures" VALUES (' + ", ".join(esc(v) for v in vals) + ");")

            if played and f["match_id"]:
                path = os.path.join(args.pages, f["match_id"] + ".html")
                if not os.path.exists(path) and page:
                    time.sleep(5)
                    page.goto(f["url"], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_selector("#meta", timeout=180000)
                    open(path, "w", encoding="utf-8").write(page.content())
                if os.path.exists(path):
                    for side, minute, typ, player, assist in parse_events(read(path)):
                        sql.append('INSERT INTO "fixture_events" VALUES (' +
                                   ", ".join(esc(v) for v in [eid, side, minute, typ, player, assist]) + ");")
                else:
                    missing.append(f"{f['match_id']}\t{eid}\t{f['home']['short']} v {f['away']['short']}\t{f['url']}")
    if args.inserts_only:
        def batch(head, rows_, tail=";", n=100):
            return [head + "\n" + ",\n".join(rows_[i:i + n]) + tail for i in range(0, len(rows_), n)]
        lg_rows = ["(%d, %s, %s, %s, %s, %s)" % (lid, esc(pg["league"]), esc(pg["league"]), esc(pg["season"]),
                                                esc(pg["logo"]), esc(re.sub(r"[^a-z0-9]+", "-", pg["league"].lower()).strip("-")))
                   for lid, pg in sorted(leagues.items())]
        vn_rows = ["(%d, %s)" % (vid, esc(c.most_common(1)[0][0])) for vid, c in sorted(venue_names.items())]
        ids = ", ".join("'%d'" % l for l in sorted(leagues))
        ins = ["-- some abbreviations have 4 letters (COMO, ROMA): widen the two columns first (harmless to run again)",
               'ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), ALTER COLUMN "away_team_abbr" TYPE varchar(10);',
               "\n-- leagues (skipped if the id already exists)"]
        ins += batch('INSERT INTO "leagues" ("leagueid", "name", "shortname", "season", "logourl", "slug") VALUES', lg_rows,
                     '\nON CONFLICT ("leagueid") DO NOTHING;')
        ins += ["\n-- venues (skipped if the id already exists)"]
        ins += batch('INSERT INTO "venues" ("venueid", "fullname") VALUES', vn_rows, '\nON CONFLICT ("venueid") DO NOTHING;')
        ins += [f'\n-- fixtures (first removes any earlier rows of these leagues, so running it twice does not duplicate)',
                f'DELETE FROM "fixtures" WHERE "league_id" IN ({ids});']
        ins += batch('INSERT INTO "fixtures" ("rn", "season_type", "league_id", "event_id", "date", "venue_id", "attendance", '
                     '"home_team_id", "away_team_id", "home_team_winner", "away_team_winner", "home_team_score", "away_team_score", '
                     '"home_team_shootout_score", "away_team_shootout_score", "status_id", "update_time", "home_team_abbr", '
                     '"home_team_name", "away_team_abbr", "away_team_name") VALUES', fx_rows)
        open(out("inserts.sql"), "w", encoding="utf-8").write("\n".join(ins) + "\n")
        print(f"{len(pages)} league(s), {len(fx_rows)} fixtures ({played_n} played), {len(venue_names)} venues\nwrote {out('inserts.sql')}")
        return
    sql.append(VIEW)
    open(out("fixtures.sql"), "w", encoding="utf-8").write("\n".join(sql) + "\n")
    open(out("missing_matches.txt"), "w", encoding="utf-8").write("\n".join(missing))
    print(f"{len(pages)} league(s), {eid} fixtures ({played_n} played), {len(team_of)} teams matched, "
          f"{len(venue_names)} venues | events missing for {len(missing)} played matches\nwrote {args.out}/")


if __name__ == "__main__":
    main()
