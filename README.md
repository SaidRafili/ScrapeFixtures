# FBref -> Neon fixtures sync

GitHub Actions runs `sync.py` twice a day. It downloads each FBref "Scores & Fixtures" page listed in
`leagues.json`, builds the SQL with `fbref_matches.py`, and loads it into the `fixtures` table in Neon
(DELETE + INSERT per league, in one transaction).

## Set up
1. Put these files in the repo root next to `sync.py`: **`fbref.py`**, **`teams.csv`**, and `team_overrides.json` if you use one.
2. Edit `leagues.json`: one entry per league. Give every league a different `start_id` (0, 1000, 2000, ...)
   so `event_id` / `rn` never collide.
3. GitHub repo -> Settings -> Secrets and variables -> Actions -> New secret `DATABASE_URL` = your Neon
   connection string (the pooled one is fine; it must end with `?sslmode=require`).
4. Actions tab -> "FBref -> Neon fixtures sync" -> Run workflow, and read the log.

## Test locally
    python sync.py --html Ligue_1.html --league "Ligue 1" --dry-run     # writes sql/ only
    DATABASE_URL=... python sync.py --league "Ligue 1"
