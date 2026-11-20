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
