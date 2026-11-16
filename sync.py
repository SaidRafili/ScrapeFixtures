#!/usr/bin/env python3
"""
sync.py - open FBref "Scores & Fixtures" pages in a real browser, build the SQL with fbref_matches.py
and load it into Neon.

    1. put the league links in leagues.txt (one per line)       <- the only thing you configure
    2. DATABASE_URL=postgres://... python sync.py
       python sync.py --dry-run                                  # fetch + build SQL into ./sql, touch no database
       python sync.py --html Ligue_1.html --dry-run              # use a page you saved yourself, no browser
       python sync.py https://fbref.com/en/comps/9/schedule/... # links on the command line instead of leagues.txt

Per league the script works out everything itself:
  * league id  = the number in the link (/comps/13/...)
  * start id   = league id * 10000, so event_id / rn are stable and can never collide between leagues
  * season     = read from the page

FBref sits behind a bot check ("Just a moment..."): plain HTTP requests get a 403. So the pages are opened in
a REAL (visible) Chromium, one browser for the whole run so the check is passed only once. On a server there is
no screen: run it under a virtual one (`xvfb-run -a python sync.py`, the GitHub workflow already does).
HEADLESS=1 forces headless mode (usually blocked).

Safety: every league loads in ONE transaction; it is skipped (nothing deleted) if the page can not be fetched,
0 matches parse, or the played matches would shrink by >20% vs the table (FORCE=1 overrides that).
"""
import argparse, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DELAY = 8          # seconds between FBref pages (their limit is 10 requests/minute)
TRIES = 3


def read_links(path, cli):
    links = list(cli)
    if not links and os.path.exists(path):
        links = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.strip().startswith("#")]
    out = []
    for u in links:
        m = re.search(r"fbref\.com/\w+/comps/(\d+)/", u)
        if not m:
            sys.exit(f"Not an FBref league link: {u}\n(expected https://fbref.com/en/comps/<id>/schedule/...-Scores-and-Fixtures)")
        out.append((u, int(m.group(1))))
    if not out:
        sys.exit(f"No league links. Add them to {path} (one per line).")
    return out


class Browser:
    """One real Chromium for the whole run, so FBref's bot check is passed once and the cookie is reused."""
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.b = self.pw.chromium.launch(headless=os.environ.get("HEADLESS") == "1",
                                         args=["--disable-blink-features=AutomationControlled"])
        self.page = self.b.new_context(locale="en-US", viewport={"width": 1366, "height": 900}).new_page()
        return self

    def __exit__(self, *a):
        self.b.close(); self.pw.stop()

    def get(self, url):
        from playwright.sync_api import TimeoutError as PWTimeout
        last = ""
        for n in range(1, TRIES + 1):
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                # the bot check page is replaced by the real page after a few seconds
                self.page.wait_for_selector("table[id^=sched_]", state="attached", timeout=90_000)
                return self.page.content()
            except PWTimeout:
                last = f"timeout, page title: {self.page.title()!r}"
            except Exception as e:
                last = str(e).splitlines()[0]
            print(f"  attempt {n}/{TRIES} failed ({last})", flush=True)
            time.sleep(15 * n)
        raise RuntimeError(f"could not get the fixtures table: {last}")


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
        print(f"  {name}: 0 matches parsed, skipping (nothing deleted)"); return
    with psycopg.connect(url) as conn:                       # `with` = one transaction, rolled back on error
        d = re.search(r'^DELETE FROM "fixtures" (WHERE .*);$', played, re.M)
        if d and not force:
            old = conn.execute(f'SELECT count(*) FROM "fixtures" {d.group(1)}').fetchone()[0]
            if old and new_played < old * 0.8:
                print(f"  {name}: played would drop {old} -> {new_played}; skipping. Re-run with FORCE=1 if that is right.")
                return
        conn.execute(played)
        conn.execute(unplayed)
    print(f"  {name}: loaded {new_played} played + {n_matches(unplayed)} unplayed")


def run_one(label, html_path, comp_id, args, url):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(HERE, "sql") if args.dry_run else os.path.join(tmp, "sql")
        sqls = build_sql(html_path, out, comp_id * 10000, args.teams, args.overrides)
        if not args.dry_run:
            load(url, label, sqls, os.environ.get("FORCE") == "1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("links", nargs="*", help="FBref league links (default: the ones in leagues.txt)")
    ap.add_argument("--leagues", default=os.path.join(HERE, "leagues.txt"))
    ap.add_argument("--teams", default=os.path.join(HERE, "teams.csv"))
    ap.add_argument("--overrides", default=os.path.join(HERE, "team_overrides.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--html", help="saved page to use instead of opening FBref (league id is read from it)")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url and not args.dry_run:
        sys.exit("Set DATABASE_URL (your Neon connection string) or use --dry-run")

    if args.html:
        import fbref
        run_one(os.path.basename(args.html), args.html, fbref.parse_page(args.html)["league_id"], args, url)
        return

    failed, links = [], read_links(args.leagues, args.links)     # validate the links before opening a browser
    with Browser() as br:
        for i, (link, comp_id) in enumerate(links):
            print(f"\n== {link}", flush=True)
            try:
                if i: time.sleep(DELAY)
                with tempfile.TemporaryDirectory() as tmp:
                    html_path = os.path.join(tmp, "page.html")
                    open(html_path, "w", encoding="utf-8").write(br.get(link))
                    run_one(link.rstrip("/").rsplit("/", 1)[-1], html_path, comp_id, args, url)
            except Exception as e:                 # one broken league must not stop the others
                print(f"  FAILED: {e}"); failed.append(link)
    if failed:
        sys.exit("\nFailed:\n  " + "\n  ".join(failed))


if __name__ == "__main__":
    main()
