# FBref -> `fixtures` sync (soccerdata)

`sync.py` uses the [soccerdata](https://github.com/probberechts/soccerdata) library to read each league's FBref
schedule, matches the teams to `teams.csv`, and loads everything into the SQL table `fixtures`
(DELETE + INSERT per league and season, in one transaction). No browser or HTML parsing any more.

## Set up
1. Edit `leagues.json` - one object per league (`league` = soccerdata id, `comp_id` = FBref competition number,
   `country` = slug prefix in teams.csv, `tz` = venue time zone, `season_style` = `split` or `calendar`).
   The season is picked automatically (July onwards = new season) or forced with `--season 2025-2026`.
2. Add `team_overrides.json` (`{"<team name as soccerdata gives it>": <teamId>}`) only if a team fails to match.
3. Set `DATABASE_URL` (Postgres / Neon). First run: add `--create-table` if `fixtures` doesn't exist yet.
4. GitHub: Settings -> Secrets -> Actions -> `DATABASE_URL` (optional `PROXY`), then run the workflow.

## Run locally
    pip install -r requirements.txt
    python sync.py --dry-run                       # writes ./out/<league>.csv, touches no database
    DATABASE_URL=... python sync.py --create-table
    python sync.py --league "ENG-Premier League" --season 2025-2026

## Notes
* `event_id` / `rn` = comp_id x 10000 + position when sorted by FBref's match id, so they stay stable when dates move.
* Kick-off times are FBref's venue-local times converted to UTC via `tz`; matches without a time get `00:00:00+00`.
* Safety: a league is skipped (nothing deleted) if the scrape fails, returns 0 matches, or the played matches would
  shrink by >20% vs the table (`FORCE=1` overrides).
* Other leagues: soccerdata only knows its built-in FBref leagues; add custom ones via `league_dict.json`
  in `~/soccerdata/config/` (see the soccerdata docs).

## ESPN sync (`sync_espn.py`, the one the workflow runs)
`leagues.json` now covers the top-5 leagues, the Turkish Süper Lig (`tur.1`), and national-team competitions
(`fifa.friendly`, World Cup + qualifiers, Nations League, Euro + qualifying, Copa America, AFCON, Asian Cup, Gold Cup).
* National competitions use `"season_style": "calendar"` (label = year, e.g. `2026`) and `comp_id` 800+.
* Entries with `"optional": true` only warn if ESPN rejects the slug, instead of failing the run.
* Azerbaijan: ESPN does not appear to carry the Azerbaijan Premier League (only Qarabag via UEFA competitions),
  so `aze.1` is a best-effort optional entry. Azerbaijan's national team is included via the competitions above.
  Check with `python sync_espn.py --find aze`; if a real slug shows up, put it in `leagues.json`.
* Clubs/teams missing from `teams.csv` are listed in the log (`note: N team(s) not in teams.csv`).

## European + continental competitions
`leagues.json` also includes (all `"optional": true`, so a wrong/unsupported ESPN slug only warns): UEFA Champions League (`uefa.champions`, id 8),
Europa League (`uefa.europa`, 19), Conference League (`uefa.europa.conf`, 882), Super Cup, the three qualifying rounds, FIFA Club World Cup,
Copa Libertadores / Sudamericana, CONCACAF Champions Cup, AFC Champions League, CAF Champions League, and domestic cups
(FA Cup, EFL Cup, Copa del Rey, DFB-Pokal, Coppa Italia, Coupe de France, Turkish Cup). Club competitions use `split` seasons (`2025/26`),
CWC / Libertadores / Sudamericana / CONCACAF use `calendar`. Verify any slug with `python sync_espn.py --find champions`, and fix or delete lines that don't exist.

## League names (`leagues` table)
`sync_espn.py` reads each league's name and logo from ESPN's own scoreboard response and saves them in `leagues` (`leagues.json` `"name"` is only the
fallback), so new competitions show "FIFA World Cup" instead of "League 801" with no manual step. A real name already in the table is never overwritten;
a blank or "League 801"-style row is filled in. `--dry-run` writes `out/leagues.csv`.

## Venue names + photos (`venues` table)
`venues` = `venueid`, `fullname`, `photourl`, `photocredit`. Two steps fill it:
1. **`sync_espn.py` saves every venue it sees** (id + name from the ESPN scoreboard, `INSERT ... ON CONFLICT DO NOTHING`, so a name or photo you
   corrected by hand is never overwritten). `--dry-run` writes `out/venues.csv`. International matches (friendlies, qualifiers...) often arrive with a venue name but no venue id: those get a stable id
   made from the name, so national-team grounds are saved too. Venues of teams that play in none of the leagues in `leagues.json` are not seen.
2. **`sync_venue_photos.py` finds a photo on Wikipedia / Wikimedia Commons** and saves Wikimedia's own *thumbnail* link (1280px by default, never the
   multi-MB original) plus the author/licence line in `photocredit`. Only free Commons files are used, never logos/crests/maps.

        pip install "psycopg[binary]>=3.2"
        python sync_venue_photos.py --lookup "Emirates Stadium" --team Arsenal    # try the search on one name, no database
        python sync_venue_photos.py --csv venues.csv --dry-run                    # try a whole CSV, writes out/venue_photos.csv
        DATABASE_URL=... python sync_venue_photos.py --limit 150                  # fill venues without a photo, busiest first
        DATABASE_URL=... python sync_venue_photos.py --shrink                     # 3840px links already saved -> 1280px (no search)
        DATABASE_URL=... python sync_venue_photos.py --replace-external           # swap stadiumdb.com etc. photos for Wikipedia ones

Venues with no match are remembered in `venue_photo_misses` and retried after 60 days. Set `WIKI_UA="Your site (you@example.com)"` (Wikimedia asks for
a contact). GitHub: `venue-photos.yml` runs it weekly (secrets: `DATABASE_URL`, optional `WIKI_UA`). **Always spot-check the first results**: a search can
pick the wrong stadium; fix one with `UPDATE venues SET photourl='...', photocredit='...' WHERE venueid=...`.

## A team's matches are missing (e.g. France)
Which seasons are loaded: a normal run loads only the **current** season (national competitions: the current calendar year) and the scheduled run
(`--upcoming`) only last week + this week + next week. Older matches need a one-off backfill:

    python sync_espn.py --season 2024,2025,2025-2026          # several seasons, one after the other (add --league fifa.friendly to limit it)
    # GitHub: Actions -> "ESPN -> fixtures sync" -> Run workflow -> mode: full, season: 2024,2025,2025-2026

To see what the database has for a team (France = 478; the women's team is 2755), run:

    SELECT league_id, season_type, count(*) AS matches, min(date), max(date)
      FROM fixtures WHERE '478' IN (home_team_id, away_team_id) GROUP BY 1, 2 ORDER BY 2 DESC, 1;

* No rows for a season/league -> they were never loaded: backfill as above.
* Rows exist but the site does not show them -> the site only lists FINISHED matches (status 28 / 45 / 46 / 47) and has to match the fixture's team
  id to a row of `teams` (Futlog `resolveTeams`, which prefers the team's home ground); check the match status and that team 478 is in `teams`.

National venue photos: `python sync_venue_photos.py --national --limit 0` looks up every ground used by a national competition (league ids 800+)
first; the weekly workflow does this before the general run.

## Better venue photos (`venue_resolver.py`) - national-team grounds
`sync_venue_photos.py` now asks `venue_resolver.resolve()` first, then falls back to the old English-Wikipedia search:
1. **Wikidata** entity search (any language + aliases), must be a stadium/arena class and agree with ESPN's city/country -> picture `P18`, panorama `P4291`, or a file in its Commons category.
2. **Commons geosearch**: free photos taken within 250 m of the stadium's coordinates.
3. **Commons text search** `"<name> stadium"`.
`sync_espn.py` now also stores ESPN's `city` and `country` in `venues` (new columns, filled only when blank) so two grounds with the same name are told apart.
Result lines show which route worked: `[wikidata image]`, `[commons geosearch]`, ...

## Match photos for posters (`sync_match_photos.py`)
    python sync_match_photos.py --lookup "Azerbaijan" "Sweden" --date 2025-09-09
    DATABASE_URL=... python sync_match_photos.py --national --limit 50
Only **Wikimedia Commons** files with CC0 / public domain / CC BY / CC BY-SA licences, taken on the match date (+/- 1 day) and naming one of the teams.
Credit + licence are stored in `match_photos` and **must be shown** on the poster (small print). Never scrape ESPN / Getty / AP / federation photos.
Most matches have no free photo: keep a fallback poster (stadium photo + your own graphics). Team crests/logos are trademarks: check your rights separately.
