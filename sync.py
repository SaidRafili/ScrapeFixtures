#!/usr/bin/env python3
"""
sync.py - fetch FBref "Scores & Fixtures" pages, build the SQL with fbref_matches.py and load it into Neon.

    DATABASE_URL=postgres://... python sync.py                 # all leagues in leagues.json
    python sync.py --dry-run                                   # fetch + build SQL only, touch nothing
    python sync.py --html Ligue_1.html --league "Ligue 1"      # use a saved page instead of fetching

Safety:
  * Each league is loaded in ONE transaction (DELETE + INSERT for played and unplayed together), so the
    table is never left half-empty if something fails.
  * A league is skipped (nothing deleted) if the fetch fails, 0 matches parse, or the played matches
    would shrink by more than 20% versus what the table already has. Set FORCE=1 to override the shrink check.
  * start_id of every league in leagues.json must be spaced further apart than its match count
    (use 0, 1000, 2000, ...) because event_id / rn = start_id + position and must not collide.
"""
import argparse, json, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
DELAY = 8   # seconds between FBref requests (their limit is 10 requests/minute)


def fetch(url):
    import requests
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=60)
    if r.status_code == 200 and "<table" in r.text:
        return r.text
    print(f"  plain request gave HTTP {r.status_code}; trying a headless browser", flush=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(user_agent=UA, locale="en-US").new_page()
        resp = pg.goto(url, wait_until="domcontentloaded", timeout=90_000)
        pg.wait_for_timeout(4000)
        html = pg.content()
        status = resp.status if resp else 0
        b.close()
    if "<table" not in html or status >= 400:
        raise RuntimeError(f"FBref blocked the request (HTTP {status})")
    return html


def build_sql(html_path, out_dir, start_id, teams, overrides):
    cmd = [sys.executable, os.path.join(HERE, "fbref_matches.py"), html_path, "--teams", teams,
           "--overrides", overrides, "--out", out_dir, "--start-id", str(start_id)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    print(r.stdout.strip())
    if r.returncode:
        raise RuntimeError("fbref_matches.py failed:\n" + r.stdout + r.stderr)
    return {k: open(os.path.join(out_dir, f"{k}_inserts.sql"), encoding="utf-8").read() for k in ("played", "unplayed")}


def n_matches(sql):
    m = re.search(r"^-- \w+: .*\((\d+) matches\)", sql, re.M)
    return int(m.group(1)) if m else 0


def load(url, name, sqls, force):
    import psycopg
    played, unplayed = sqls["played"], sqls["unplayed"]
    new_played = n_matches(played)
    if new_played + n_matches(unplayed) == 0:
        print(f"  {name}: 0 matches parsed, skipping (nothing deleted)"); return False
    with psycopg.connect(url) as conn:                       # `with` = one transaction, rolled back on error
        d = re.search(r'^DELETE FROM "fixtures" (WHERE .*);$', played, re.M)
        if d and not force:
            old = conn.execute(f'SELECT count(*) FROM "fixtures" {d.group(1)}').fetchone()[0]
            if old and new_played < old * 0.8:
                print(f"  {name}: played would drop {old} -> {new_played}; skipping. Re-run with FORCE=1 if that is right.")
                return False
        conn.execute(played)
        conn.execute(unplayed)
    print(f"  {name}: loaded {new_played} played + {n_matches(unplayed)} unplayed")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "leagues.json"))
    ap.add_argument("--teams", default=os.path.join(HERE, "teams.csv"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "team_overrides.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--html", help="saved FBref page to use instead of fetching (needs --league)")
    ap.add_argument("--league", help="only this league name from leagues.json")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url and not args.dry_run:
        sys.exit("Set DATABASE_URL (your Neon connection string) or use --dry-run")
    leagues = [l for l in json.load(open(args.config)) if not args.league or l["name"] == args.league]
    if not leagues:
        sys.exit("No matching league in leagues.json")
    force = os.environ.get("FORCE") == "1"
    failed = []

    for i, lg in enumerate(leagues):
        print(f"\n== {lg['name']}", flush=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if args.html:
                    html_path = args.html
                else:
                    if i: time.sleep(DELAY)
                    html_path = os.path.join(tmp, "page.html")
                    open(html_path, "w", encoding="utf-8").write(fetch(lg["url"]))
                out = os.path.join(HERE, "sql") if args.dry_run else os.path.join(tmp, "sql")
                sqls = build_sql(html_path, out, lg.get("start_id", 0), args.teams, args.overrides)
                if not args.dry_run:
                    load(url, lg["name"], sqls, force)
        except Exception as e:                 # one broken league must not stop the others
            print(f"  FAILED: {e}"); failed.append(lg["name"])
    if failed:
        sys.exit(f"\nFailed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
