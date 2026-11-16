# FBref -> Neon fixtures sync

GitHub Actions runs `sync.py` twice a day. It opens each FBref "Scores & Fixtures" link from **`leagues.txt`**
in a real browser (FBref blocks plain requests), builds the SQL with `fbref_matches.py` and loads it into the
`fixtures` table in Neon (DELETE + INSERT per league, in one transaction).

## Set up
1. Add your league links to `leagues.txt`, one per line. That is all the configuration there is. League id, season and
   the event_id range (league id x 10000) are worked out automatically.
2. `teams.csv` and `fbref.py` are already here. Add `team_overrides.json` ({"<fbref squad id>": <teamId>}) only if a team fails to match.
3. GitHub repo -> Settings -> Secrets and variables -> Actions -> New secret `DATABASE_URL` = your Neon connection string.
4. Actions tab -> "FBref -> Neon fixtures sync" -> Run workflow, and read the log.

## Run on your own computer
    pip install -r requirements.txt && playwright install chromium
    python sync.py --dry-run                      # writes ./sql only
    DATABASE_URL=... python sync.py
