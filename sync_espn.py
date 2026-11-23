#!/usr/bin/env python3
"""
sync_espn.py - load league fixtures from ESPN's public JSON API into the SQL table `fixtures`.

No browser, no captcha, no HTML parsing, no team-name matching: ESPN's team ids ARE the ids in teams.csv.

    DATABASE_URL=postgres://... python sync_espn.py              # all leagues in leagues.json, current season
    python sync_espn.py --dry-run                                # write ./out/<league>.csv, touch no database
    python sync_espn.py --league eng.1 --season 2025-2026
    python sync_espn.py --create-table

leagues.json (one object per league):  {"espn": "eng.1", "comp_id": 9, "season_style": "split"}
    espn          ESPN league slug (eng.1, esp.1, tur.1, fifa.friendly, uefa.nations ...)
    comp_id       written to league_id (kept equal to the old FBref number so existing rows are replaced)
    season_style  "split" (Jul-Jun, label 2026/27) or "calendar" (Jan-Dec, label 2026; used for national teams)
    name          optional, only for the log
    optional      optional; true = if ESPN rejects the slug / has no such league, warn and carry on (no red run)

    python sync_espn.py --find azer       # list ESPN league slugs containing "azer" (to check a slug exists)

    python sync_espn.py --upcoming                # window mode (what the scheduled run uses): last week + this week + next week only
    python sync_espn.py --upcoming --weeks-back 0 --weeks-ahead 2

--upcoming: weeks run Monday 00:00 -> Monday 00:00 in Baku time (WEEK_OFFSET_HOURS, the same weeks the site's Predictions page uses). Only fixtures in the
window are fetched and replaced (per league: DELETE + INSERT of that window in one transaction), so it is fast and the rest of the
season is left alone. Last week is included so its final scores arrive (they decide the weekly leaderboard).

--season: "2025-2026" applies to split leagues only, "2025" to calendar leagues only (others are skipped). Without it only the CURRENT season / year is
loaded, so older national-team matches (e.g. France's 2025 World Cup qualifiers) need a backfill:  python sync_espn.py --season 2024,2025,2025-2026
Several values run one after the other (several seasons of every league, not just the national ones; add --league fifa.friendly to limit it).

Leagues: each league's name and logo are read from ESPN's scoreboard (leagues.json "name" is only the fallback) and saved in `leagues`, so the site shows
"FIFA World Cup" instead of "League 801". Existing real names are never overwritten.

Venues: every venue id in the scoreboards is also saved (id + name) in `venues` (never overwriting a row), so each match shows its stadium. International matches often come without a venue id: those get a stable
id of our own made from the venue name (synthetic_venue_id), so national-team grounds are saved too;
sync_venue_photos.py then adds the photos.

Per league+season: DELETE + INSERT in ONE transaction. Skipped (nothing deleted) if the fetch fails, returns
0 matches, or finished matches would shrink >20% vs the table (FORCE=1 overrides).
"""
import argparse, calendar, csv, json, os, re, sys, time, zlib
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

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
VENUES_DDL = """CREATE TABLE IF NOT EXISTS "venues" (
\t"venueid" integer PRIMARY KEY,
\t"fullname" varchar(150) NOT NULL,
\t"photourl" varchar(500) DEFAULT '' NOT NULL,
\t"photocredit" varchar(255) DEFAULT '' NOT NULL
);"""
LEAGUES_DDL = """CREATE TABLE IF NOT EXISTS "leagues" (
\t"leagueid" integer PRIMARY KEY,
\t"name" varchar(100) NOT NULL,
\t"shortname" varchar(50) NOT NULL,
\t"season" varchar(20) NOT NULL,
\t"logourl" varchar(255) NOT NULL,
\t"slug" varchar(100) NOT NULL
);"""
LEAGUE_INFO = {}       # ESPN slug -> the "leagues[0]" object of its scoreboard (name, abbreviation, logos); saved by save_leagues()
VENUES = {}            # venue id -> name, collected from every scoreboard event we parse; saved by save_venues()
RECENT_DAYS = 3        # finished matches this recent are re-fetched every run (VAR / late corrections)


# ------------------------------------------------------------------ seasons
def season_bounds(style, today=None, override=None):
    """-> (start date, end date, label). split: Jul 1 - Jun 30; calendar: Jan 1 - Dec 31."""
    today = today or datetime.now(timezone.utc).date()
    if style == "calendar":
        if override and not re.fullmatch(r"\d{4}", override):
            return None                                     # a 2025-2026 override does not apply to calendar leagues
        y = int(override) if override else today.year
        return date(y, 1, 1), date(y, 12, 31), str(y)
    if override:
        if not re.fullmatch(r"\d{4}-\d{4}", override):
            return None                                     # a plain year does not apply to split leagues
        y = int(override[:4])
    else:
        y = today.year if today.month >= 7 else today.year - 1
    return date(y, 7, 1), date(y + 1, 6, 30), f"{y}/{str(y + 1)[2:]}"


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = y + (m == 12), m % 12 + 1


WEEK_OFFSET_HOURS = 4      # weeks start Monday 00:00 in this zone (4 = Baku, UTC+4). Keep equal to WEEK_OFFSET_HOURS in Futlog's lib/predictions.js


def week_window(back=1, ahead=1, now=None):
    """-> (start, end) datetimes (UTC, end exclusive): `back` weeks before the current week up to the end of `ahead` weeks after it."""
    off = timedelta(hours=WEEK_OFFSET_HOURS)
    local = (now or datetime.now(timezone.utc)) + off                      # clock time in the week zone
    monday = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) - timedelta(days=local.weekday()) - off
    return monday - timedelta(weeks=back), monday + timedelta(weeks=ahead + 1)


def event_label(style, d):
    """Season label of one match date (a window can straddle the July season change)."""
    if style == "calendar":
        return str(d.year)
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}/{str(y + 1)[2:]}"


def ts_text(d):
    return d.strftime("%Y-%m-%d %H:%M:%S+00")


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


def _events(data, slug=None):
    if slug and slug not in LEAGUE_INFO:
        lg = (data.get("leagues") or [None])[0]             # ESPN sends the league's own name + logo with every scoreboard
        if isinstance(lg, dict) and lg.get("name"):
            LEAGUE_INFO[slug] = lg
    return data.get("events") or []


def fetch_month(slug, y, m):
    """ESPN no longer accepts date ranges (dates=A-B -> 400), only dates=YYYYMMDD or dates=YYYYMM.
    Ask for the month; if that is refused or may be truncated, ask day by day."""
    url = f"{BASE}/{slug}/scoreboard?dates={y}{m:02d}"
    for extra, cap in (("&limit=1000", 1000), ("", 100)):
        try:
            evs = _events(get_json(url + extra), slug)
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
            out += _events(get_json(f"{BASE}/{slug}/scoreboard?dates={y}{m:02d}{day:02d}"), slug)
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


def fetch_window(slug, start, end):
    """Events whose kick-off is in [start, end): fetches the months the window touches, keeps only the window."""
    events = {}
    for y, m in months(start.date(), (end - timedelta(seconds=1)).date()):
        for ev in fetch_month(slug, y, m):
            try:
                d = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
            except (KeyError, ValueError):
                continue
            if start <= d < end:
                events[str(ev["id"])] = ev
        time.sleep(0.3)
    return list(events.values())


# ------------------------------------------------------------------ parse
def synthetic_venue_id(name):
    """ESPN often sends international matches (friendlies, qualifiers) with a venue NAME but no venue id. Rather than lose the venue, give it a
    stable id of our own from the name (1.0 - 2.0 billion, far above ESPN's real ids), so the same ground always gets the same id."""
    return 1_000_000_000 + zlib.crc32(name.lower().encode("utf-8")) % 1_000_000_000


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
    vname = " ".join(str(ven.get("fullName") or "").split())[:150]
    vid = ven.get("id") if str(ven.get("id") or "").isdigit() else (synthetic_venue_id(vname) if vname else None)
    if vid and vname:
        VENUES[int(vid)] = vname
    return [None, label, cfg["comp_id"], ev["id"], d.strftime("%Y-%m-%d %H:%M:%S+00"), vid,
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


def build_window_rows(events, cfg, now, rn_map, top_rn):
    """Like build_rows, but the season label comes from each match's own date and `rn` keeps the value a row already has in the
    table (new matches get top_rn + 1, +2 ...), so rows outside the window never collide with these."""
    style = cfg.get("season_style", "split")
    rows = []
    for e in events:
        d = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
        r = parse_event(e, cfg, event_label(style, d), now)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: (int(r[3]) if str(r[3]).isdigit() else 0, r[3]))
    for r in rows:
        if r[3] in rn_map:
            r[0] = rn_map[r[3]]
        else:
            top_rn += 1
            r[0] = top_rn
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


def existing_rn(url, cfg):
    """-> ({event_id: rn} already in the table for this league, highest rn in use)."""
    base = int(cfg["comp_id"]) * 10000
    if not url:
        return {}, base
    import psycopg
    with psycopg.connect(url) as conn:
        got = conn.execute('SELECT "event_id", "rn" FROM "fixtures" WHERE "league_id" = %s', (str(cfg["comp_id"]),)).fetchall()
    rn_map = {e: r for e, r in got if r}
    return rn_map, max([int(r) for r in rn_map.values() if str(r).isdigit()] or [base])


def load_window(url, cfg, rows, start, end, force, create_table, ev_rows=(), ev_ids=()):
    """Replace this league's fixtures inside [start, end) only. Matches that moved out of the window are removed by event id."""
    import psycopg
    played = sum(r[COLUMNS.index("status_id")] in FINISHED for r in rows)
    lo, hi = ts_text(start), ts_text(end)
    with psycopg.connect(url) as conn:
        if create_table:
            conn.execute(DDL)
            conn.execute(EVENTS_DDL)
        conn.execute('ALTER TABLE "fixtures" ALTER COLUMN "home_team_abbr" TYPE varchar(10), '
                     'ALTER COLUMN "away_team_abbr" TYPE varchar(10)')
        league = str(cfg["comp_id"])
        if not force:
            old = conn.execute('SELECT count(*) FROM "fixtures" WHERE "league_id" = %s AND "date" >= %s AND "date" < %s '
                               'AND "status_id" = ANY(%s)', (league, lo, hi, list(FINISHED))).fetchone()[0]
            if old and played < old * 0.8:
                print(f"  finished in the window would drop {old} -> {played}; skipping. Re-run with FORCE=1 if that is right.")
                return
        conn.execute('DELETE FROM "fixtures" WHERE "league_id" = %s AND (("date" >= %s AND "date" < %s) OR "event_id" = ANY(%s))',
                     (league, lo, hi, [r[3] for r in rows]))
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        with conn.cursor() as cur:
            cur.executemany(f'INSERT INTO "fixtures" ({cols}) VALUES ({", ".join(["%s"] * len(COLUMNS))})', rows)
        if ev_ids:
            conn.execute('DELETE FROM "fixture_events" WHERE "event_id" = ANY(%s)', (list(ev_ids),))
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO "fixture_events" ("event_id","team_side","minute","event_type","player","assist") '
                                'VALUES (%s,%s,%s,%s,%s,%s)', ev_rows)
    print(f"  window loaded: {played} finished + {len(rows) - played} upcoming/other, {len(ev_rows)} goal/card events for {len(ev_ids)} matches")


# ------------------------------------------------------------------ main
def league_rows(leagues):
    """[(comp_id, name, shortname, season, logo, slug)] for the leagues of this run: ESPN's own name first, leagues.json "name" as fallback."""
    out = []
    for cfg in leagues:
        info = LEAGUE_INFO.get(cfg["espn"]) or {}
        name = " ".join(str(info.get("name") or cfg.get("name") or "").split())[:100]
        if not name:
            continue
        logos = [l for l in info.get("logos") or [] if l.get("href")]
        logo = next((l["href"] for l in logos if "default" in (l.get("rel") or [])), logos[0]["href"] if logos else "")
        short = " ".join(str(info.get("abbreviation") or name).split())[:50]
        out.append((int(cfg["comp_id"]), name, short, "", logo[:255], cfg["espn"][:100]))
    return out


def save_leagues(url, rows):
    """Add each league's name / logo to `leagues` (read by the site as "Premier League" instead of "League 9"). A row that already has a real name is
    left alone; a blank or placeholder ("League 801") one is filled in. Own connection, so a failure here never affects the fixtures."""
    if not rows:
        return
    import psycopg
    try:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(LEAGUES_DDL)
            cur.executemany("""INSERT INTO "leagues" ("leagueid", "name", "shortname", "season", "logourl", "slug") VALUES (%s, %s, %s, %s, %s, %s)
                               ON CONFLICT ("leagueid") DO UPDATE SET "name" = EXCLUDED."name", "shortname" = EXCLUDED."shortname",
                                 "logourl" = CASE WHEN "leagues"."logourl" = '' THEN EXCLUDED."logourl" ELSE "leagues"."logourl" END,
                                 "slug" = CASE WHEN "leagues"."slug" = '' THEN EXCLUDED."slug" ELSE "leagues"."slug" END
                               WHERE "leagues"."name" = '' OR "leagues"."name" ~ '^League [0-9]+$'""", rows)
            print(f"\nleagues: {len(rows)} named (rows that already had a real name are untouched)")
    except Exception as e:                                  # noqa: BLE001 - never fail the run over league names
        print(f"\nWARNING: leagues not saved: {e}")


def save_venues(url):
    """Add every venue seen in the scoreboards to `venues` (id + name) so the site can show the stadium name and sync_venue_photos.py
    has something to look up. Existing rows are never touched (a hand-corrected name or a photo stays). Own connection: if the table is
    missing or this fails, the fixtures that were already loaded are not affected."""
    if not VENUES:
        return
    import psycopg
    try:
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(VENUES_DDL)
            cur.executemany('INSERT INTO "venues" ("venueid", "fullname") VALUES (%s, %s) ON CONFLICT ("venueid") DO NOTHING',
                            sorted(VENUES.items()))
            print(f"\nvenues: {len(VENUES)} seen, {max(cur.rowcount, 0)} new (existing rows untouched)")
    except Exception as e:                                  # noqa: BLE001 - never fail the run over venue names
        print(f"\nWARNING: venues not saved: {e}")


def known_team_ids():
    path = os.path.join(HERE, "teams.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["teamId"] for r in csv.DictReader(fh)}


def report_unknown_teams(rows, known):
    """Warn about teams that are not in teams.csv (no logo / colours on the site until it is refreshed)."""
    if not known:
        return
    ix_id, ix_name = COLUMNS.index("home_team_id"), COLUMNS.index("home_team_name")
    missing = {}
    for r in rows:
        for i, n in ((ix_id, ix_name), (ix_id + 1, ix_name + 2)):
            if r[i] not in known:
                missing[r[i]] = r[n]
    if missing:
        print(f"  note: {len(missing)} team(s) not in teams.csv: "
              + ", ".join(f"{n} ({i})" for i, n in sorted(missing.items())[:15]) + (" ..." if len(missing) > 15 else ""))


def find_leagues(term):
    """List ESPN soccer league slugs containing `term` (slug is the last path part of each $ref)."""
    core = os.environ.get("ESPN_CORE", "https://sports.core.api.espn.com/v2/sports/soccer")
    data = get_json(f"{core}/leagues?limit=1000")
    slugs = sorted({re.search(r"/leagues/([^/?]+)", i["$ref"]).group(1) for i in data.get("items", []) if "$ref" in i})
    hits = [s for s in slugs if term.lower() in s.lower()]
    print(f"{len(slugs)} leagues known to ESPN; {len(hits)} match '{term}':")
    for s in hits:
        print("  ", s)


def main():
    ap = argparse.ArgumentParser(description="ESPN API -> fixtures table")
    ap.add_argument("--find", metavar="TEXT", help="list ESPN league slugs containing TEXT, then exit")
    ap.add_argument("--leagues", default=os.path.join(HERE, "leagues.json"))
    ap.add_argument("--league", help="only this ESPN slug, e.g. eng.1")
    ap.add_argument("--season", help="2025-2026 (split) or 2025 (calendar); several: 2024,2025,2025-2026")
    ap.add_argument("--upcoming", action="store_true", help="window mode: only last week + this week + next week (Mon-Mon UTC)")
    ap.add_argument("--weeks-back", type=int, default=1, help="with --upcoming: whole weeks before this one to refresh (default 1)")
    ap.add_argument("--weeks-ahead", type=int, default=1, help="with --upcoming: whole weeks after this one to fetch (default 1)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create-table", action="store_true")
    ap.add_argument("--no-events", action="store_true", help="skip goals/cards (fixture_events)")
    args = ap.parse_args()
    if args.season and "," in args.season:                  # --season 2024,2025,2026 or 2025,2025-2026: one run per season
        import subprocess
        rest, skip = [], False
        for a in sys.argv[1:]:
            if skip:
                skip = False
            elif a == "--season":
                skip = True
            elif not a.startswith("--season="):
                rest.append(a)
        rc = 0
        for one in [x.strip() for x in args.season.split(",") if x.strip()]:
            print(f"\n######## season {one}", flush=True)
            rc = subprocess.run([sys.executable, os.path.abspath(__file__), *rest, "--season", one]).returncode or rc
        sys.exit(rc)
    if args.find:
        return find_leagues(args.find)
    url = os.environ.get("DATABASE_URL")
    if not url and not args.dry_run:
        sys.exit("Set DATABASE_URL or use --dry-run")
    leagues = json.load(open(args.leagues, encoding="utf-8"))
    if args.league:
        leagues = [l for l in leagues if l["espn"] == args.league] or sys.exit(f"{args.league} not in leagues.json")
    force = os.environ.get("FORCE") == "1"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    failed, warned = [], []
    known = known_team_ids()
    win = week_window(max(args.weeks_back, 0), max(args.weeks_ahead, 0)) if args.upcoming else None
    if win:
        print(f"Window mode: {ts_text(win[0])} -> {ts_text(win[1])} (UTC, end exclusive)")
    for cfg in leagues:
        if win:
            label = f"{win[0]:%Y-%m-%d} .. {win[1]:%Y-%m-%d}"
        else:
            bounds = season_bounds(cfg.get("season_style", "split"), override=args.season)
            if bounds is None:
                print(f"\n== {cfg['espn']}: --season {args.season} does not fit its {cfg.get('season_style', 'split')} season, skipped")
                continue
            start, end, label = bounds
        print(f"\n== {cfg['espn']} {cfg.get('name', '')} {label}", flush=True)
        try:
            if win:
                rn_map, top = existing_rn(None if args.dry_run else url, cfg)
                rows = build_window_rows(fetch_window(cfg["espn"], *win), cfg, now, rn_map, top)
            else:
                rows = build_rows(fetch_events(cfg["espn"], start, end), cfg, label, now)
            if not rows:
                print("  0 matches returned, skipping (nothing deleted)")
                continue
            print(f"  {len(rows)} matches")
            report_unknown_teams(rows, known)
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
            elif win:
                load_window(url, cfg, rows, win[0], win[1], force, args.create_table, ev_rows, ev_ids)
            else:
                load(url, cfg, label, rows, force, args.create_table, ev_rows, ev_ids)
        except (Exception, SystemExit) as e:
            if cfg.get("optional"):
                print(f"  WARNING (optional league, not loaded): {e}"); warned.append(cfg["espn"])
            else:
                print(f"  FAILED: {e}"); failed.append(cfg["espn"])
    lrows = league_rows(leagues)
    if lrows:
        if args.dry_run:
            os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
            with open(os.path.join(HERE, "out", "leagues.csv"), "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh); w.writerow(["leagueid", "name", "shortname", "season", "logourl", "slug"]); w.writerows(lrows)
            print(f"\nwrote out/leagues.csv ({len(lrows)} leagues)")
        else:
            save_leagues(url, lrows)
    if VENUES:
        if args.dry_run:
            os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
            with open(os.path.join(HERE, "out", "venues.csv"), "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh); w.writerow(["venueid", "fullname"]); w.writerows(sorted(VENUES.items()))
            print(f"\nwrote out/venues.csv ({len(VENUES)} venues)")
        else:
            save_venues(url)
    if warned:
        print("\nOptional leagues that could not be loaded (check the slug with --find): " + ", ".join(warned))
    if failed:
        sys.exit("\nFailed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
