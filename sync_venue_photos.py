#!/usr/bin/env python3
"""
sync_venue_photos.py - find a stadium photo on Wikipedia / Wikimedia Commons for every row of `venues` and save a SMALL version of it.

    DATABASE_URL=postgres://... python sync_venue_photos.py --limit 150      # venues without a photo, busiest venues first
    python sync_venue_photos.py --dry-run                                    # print what would be saved, write ./out/venue_photos.csv
    python sync_venue_photos.py --national --limit 0                         # grounds of national-team matches (league ids 800+), all of them
    python sync_venue_photos.py --lookup "Emirates Stadium"                  # test the search on one name, no database needed
    python sync_venue_photos.py --csv venues.csv --dry-run                   # read venues from a CSV (venueid,fullname,photourl,photocredit)
    python sync_venue_photos.py --shrink                                     # rewrite photo links that are already saved, e.g. 3840px -> 1280px
    python sync_venue_photos.py --replace-external --limit 100               # also try to swap photos from other sites (stadiumdb...) for Wikipedia ones

How a photo is found (Wikipedia API only, no HTML scraping):
  1. search English Wikipedia for "<venue> stadium" (then "<venue> <home team>"), take the top 5 articles;
  2. accept an article only if it IS a stadium (category / short description) and its title or intro contains the venue's own words;
     a venue whose name is generic ("Stadio Olimpico") must also mention one of the teams that play there (teams.csv);
  3. take the article's lead picture - never a logo, crest, flag, map or SVG; if the lead picture is one of those, use the first other picture
     of the article whose file name looks like a stadium photo;
  4. look the file up on Wikimedia Commons: it must be a free file (pictures that exist only on en.wikipedia are often "fair use", skipped),
     at least 500px wide; get its credit line (author / licence) for the attribution Commons licences require.

Size: the saved link is Wikimedia's own thumbnail at --width pixels (default 1280, one of 330/500/960/1280/1920), never the multi-megabyte
original. The Futlog site (lib/photo.js) turns that link into a srcset (330 / 500 / 960 / 1280px) so a small card downloads ~30-60 KB.

Safe to re-run: a venue that already has a photo is not touched (unless --replace-external / --shrink says so, and a photo is only replaced when a better
one was found). Venues with no match are remembered in `venue_photo_misses` and retried after --retry-days (default 60).
Wikimedia asks every client for a contact in the User-Agent: set WIKI_UA="Futlog venue photos (you@example.com)".
"""
import argparse, csv, json, math, os, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = os.environ.get("WIKI_UA") or "ScrapeFixtures/1.0 (stadium photo importer; set WIKI_UA to your e-mail or site)"
STEPS = (330, 500, 960, 1280, 1920)            # widths Wikimedia accepts for thumbnails
MIN_ORIGINAL = 500                             # skip pictures narrower than this
WIKIMEDIA = re.compile(r"^https://(?:upload|thumb)\.wikimedia\.org/", re.I)
THUMB_W = re.compile(r"^(https://(?:upload|thumb)\.wikimedia\.org/wikipedia/[a-z-]+/thumb/[0-9a-f]/[0-9a-f]{2}/[^/]+/)(\d+)(px-[^/?#]+)(?:[?#].*)?$", re.I)

# words that say "stadium" in many languages: they do not identify WHICH stadium
GENERIC = set("""stadium stadion stade estadio stadio stadyumu stadyum stadi arena park ground grounds field campo de del della di du la le el los las the of
an am der die das und and fc cf club municipal komunal sports sport sporting complex center centre football futbol calcio fussball new nuevo nuova""".split())
BAD_FILE = re.compile(r"(?<![a-z])(logo|crest|badge|emblem|coat[ _]of[ _]arms|flag|map|seal|icon|plan|seating|diagram|banner|poster|ticket|kit|shirt|signature)(?![a-z])|\.svg$", re.I)
GOOD_EXT = re.compile(r"\.(jpe?g|png|webp)$", re.I)
PHOTO_WORDS = re.compile(r"stadium|stadion|stade|estadio|stadio|arena|ground|aerial|panorama|exterior|stand|view|from", re.I)
STADIUM_HINT = re.compile(r"stadium|stadion|stade\b|estadio|stadio|arena|football venue|football ground|sports venue|velodrome|coliseum|venues", re.I)


# ------------------------------------------------------------------ small helpers (pure)
def tokens(text):
    t = unicodedata.normalize("NFKD", str(text or "").lower().replace("ı", "i").replace("ß", "ss").replace("ø", "o").replace("đ", "d").replace("ł", "l"))
    return re.findall(r"[a-z0-9]+", "".join(c for c in t if not unicodedata.combining(c)))


def distinctive(text):
    return [t for t in dict.fromkeys(tokens(text)) if t not in GENERIC and len(t) > 1]


def clean_name(name):
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", str(name or ""))).strip()


def is_photo_file(fn):
    return bool(GOOD_EXT.search(fn)) and not BAD_FILE.search(fn)


def strip_html(h):
    h = re.sub(r"<[^>]*>", "", str(h or ""))
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&#039;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        h = h.replace(a, b)
    return re.sub(r"\s+", " ", h).strip()


def credit_line(meta):
    who, lic = strip_html((meta.get("Artist") or {}).get("value"))[:120], strip_html((meta.get("LicenseShortName") or {}).get("value"))[:40]
    out = f"{who} / Wikimedia Commons" if who else "Wikimedia Commons"
    return (f"{out}, {lic}" if lic else out)[:255]


def shrink_url(url, width=1280):
    """A Wikimedia thumbnail link wider than `width` -> the same picture at `width` (tracking query dropped). Anything else -> None (leave it)."""
    m = THUMB_W.match(url or "")
    if not m or int(m.group(2)) <= width:
        return None
    return f"{m.group(1)}{width}{m.group(3)}"


def page_matches(page, name_tokens, team_tokens):
    """Is this Wikipedia article the stadium called `name`?"""
    cats = " | ".join(c.get("title", "") for c in page.get("categories", []))
    short = (page.get("pageprops") or {}).get("wikibase-shortdesc", "")
    if not (STADIUM_HINT.search(cats) or STADIUM_HINT.search(short)):
        return False                                        # a club, a city, a person...
    hay = set(tokens(page.get("title", "") + " " + page.get("extract", "")))
    hits = [t for t in name_tokens if t in hay]
    if len(hits) < max(1, math.ceil(0.6 * len(name_tokens))):
        return False
    if len(name_tokens) <= 1 and team_tokens and not (set(team_tokens) & (hay | set(tokens(cats)))):
        return False                                        # "Stadio Olimpico" exists in Rome and in Turin: ask for the team too
    return True


# ------------------------------------------------------------------ Wikipedia / Commons
def api(host, **params):
    url = f"https://{host}/w/api.php?" + urllib.parse.urlencode({"format": "json", "formatversion": "2", **params})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for i in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            wait = int(e.headers.get("Retry-After") or 0) if e.code == 429 else 0
            err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            wait, err = 0, e
        if i == 4:
            raise RuntimeError(f"{host}: {err}")
        time.sleep(max(wait, 3 * i))


def search_pages(query):
    j = api("en.wikipedia.org", action="query", generator="search", gsrsearch=query, gsrlimit=5, gsrnamespace=0,
            prop="pageimages|categories|extracts|pageprops", piprop="name", pilimit="max", cllimit="max", clshow="!hidden",
            exintro=1, explaintext=1, exlimit="max", exchars=800, ppprop="wikibase-shortdesc")
    return sorted((j.get("query") or {}).get("pages", []), key=lambda p: p.get("index", 99))


def other_images(title, name_tokens):
    """The lead picture was a logo/map: first other file of the article that looks like a photo of the stadium."""
    j = api("en.wikipedia.org", action="query", titles=title, prop="images", imlimit=80)
    files = [i["title"].split(":", 1)[1].replace(" ", "_") for p in (j.get("query") or {}).get("pages", []) for i in p.get("images", [])]
    ok = [f for f in files if is_photo_file(f)]
    named = [f for f in ok if set(name_tokens) & set(tokens(f)) or PHOTO_WORDS.search(f)]
    return named[:3]


def commons_file(filename, width):
    """-> { url, credit, width } for a FREE Commons file, else None."""
    j = api("commons.wikimedia.org", action="query", titles="File:" + filename, prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=width)
    page = next(iter((j.get("query") or {}).get("pages", [])), None)
    if not page or page.get("missing") or not page.get("imageinfo"):
        return None                                         # only on en.wikipedia (often non-free) -> do not use
    ii = page["imageinfo"][0]
    meta = ii.get("extmetadata") or {}
    if str((meta.get("NonFree") or {}).get("value", "")).lower() == "true":
        return None
    if ii.get("mime") not in ("image/jpeg", "image/png", "image/webp") or int(ii.get("width") or 0) < MIN_ORIGINAL:
        return None
    url = ii.get("thumburl") or ii.get("url")
    if not url or len(url) > 500:
        return None
    return {"url": url, "credit": credit_line(meta), "width": int(ii.get("thumbwidth") or ii.get("width") or 0), "file": filename}


def find_photo(name, teams=(), width=1280, city="", country=""):
    """-> { url, credit, page, file } or { miss: reason }
    Tries the Wikidata / Commons-geosearch resolver first (finds national grounds whose Wikipedia title differs from ESPN's name), then the
    original English-Wikipedia article search."""
    try:
        import venue_resolver
        r = venue_resolver.resolve(name, city, country, teams, width)
        if r.get("url"):
            return r
    except Exception as e:                                  # noqa: BLE001 - fall back to the old search
        print(f"       (resolver error for {name}: {e})", flush=True)
    return find_photo_wikipedia(name, teams, width)


def find_photo_wikipedia(name, teams=(), width=1280):
    name = clean_name(name)
    name_tokens = distinctive(name)
    if not name_tokens:
        return {"miss": "name has no distinctive word"}
    team_tokens = sorted({t for team in teams for t in distinctive(team) if len(t) > 2})
    seen, reason = set(), "no matching stadium article"
    for q in (f"{name} stadium", f"{name} {teams[0]}" if teams else None):
        if not q:
            continue
        for page in search_pages(q):
            if page["title"] in seen or not page_matches(page, name_tokens, team_tokens):
                seen.add(page["title"]); continue
            seen.add(page["title"])
            lead = page.get("pageimage") or ""
            cands = ([lead] if lead and is_photo_file(lead) else []) + (other_images(page["title"], name_tokens) if not (lead and is_photo_file(lead)) else [])
            for fn in cands:
                got = commons_file(fn, width)
                if got:
                    return {**got, "page": page["title"]}
            reason = f'"{page["title"]}" matched but has no free photo'
    return {"miss": reason}


# ------------------------------------------------------------------ data
def teams_by_venue():
    """venueId -> [team names] from teams.csv (used to tell apart two stadiums with the same name)."""
    path = os.path.join(HERE, "teams.csv")
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("venueId") not in ("", "0", None):
                    out.setdefault(int(r["venueId"]), []).append(r.get("displayName") or r.get("name") or "")
    return out


def load_rows_csv(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return [dict(venueid=int(r["venueid"]), fullname=r["fullname"], photourl=r.get("photourl") or "", n=0) for r in csv.DictReader(fh)]


def db_rows(cur, replace_external, retry_days, limit, only, league_ids=None, min_league=None):
    """Venues to look up, busiest first. league_ids / min_league keep only venues that host fixtures of those leagues (national competitions
    have comp_id >= 800), so a few hundred busy club grounds can not push the national stadiums out of a limited run."""
    cur.execute("""CREATE TABLE IF NOT EXISTS venue_photo_misses (venueid integer PRIMARY KEY, tried_at timestamptz NOT NULL DEFAULT now(), reason text)""")
    cur.execute("SELECT to_regclass('public.fixtures') IS NOT NULL")
    has_fx = cur.fetchone()[0]
    if (league_ids or min_league) and not has_fx:
        sys.exit("--national / --league-ids need the fixtures table")
    has_home = False
    if has_fx:
        cur.execute("""SELECT count(*) FROM information_schema.columns WHERE table_name = 'fixtures' AND column_name = 'home_team_name' AND table_schema = ANY (current_schemas(false))""")
        has_home = cur.fetchone()[0] > 0
    join = 'LEFT JOIN "fixtures" f ON f.venue_id = v.venueid::text' if has_fx else ""
    cnt = "count(f.event_id)" if has_fx else "0"
    home = "mode() WITHIN GROUP (ORDER BY f.home_team_name)" if has_home else "NULL"      # the team that plays there most: tells two "Estadio Nacional" apart
    cond = "v.photourl = ''" + (r" OR (v.photourl ~ '^https?://' AND v.photourl !~ '^https://(upload|thumb)\.wikimedia\.org/')" if replace_external else "")
    lid = "CASE WHEN f.league_id ~ '^[0-9]+$' THEN f.league_id::int END"
    league = (f" AND {lid} = ANY(%(lids)s)" if league_ids else "") + (f" AND {lid} >= %(minl)s" if min_league else "")
    cur.execute(f"""SELECT v.venueid, v.fullname, v.photourl, {cnt} AS n, {home} AS home,
                            max(to_jsonb(v) ->> 'city') AS city, max(to_jsonb(v) ->> 'country') AS country FROM "venues" v {join}
                     WHERE v.fullname <> '' AND ({cond}){league} AND (%(only)s::int IS NOT NULL AND v.venueid = %(only)s
                        OR %(only)s::int IS NULL AND v.venueid NOT IN (SELECT venueid FROM venue_photo_misses WHERE tried_at > now() - make_interval(days => %(days)s)))
                     GROUP BY v.venueid, v.fullname, v.photourl ORDER BY n DESC, v.venueid LIMIT NULLIF(%(limit)s, 0)""",
                {"only": only, "days": retry_days, "limit": limit, "lids": league_ids or [], "minl": min_league or 0})
    return [dict(venueid=r[0], fullname=r[1], photourl=r[2], n=r[3], home=r[4], city=r[5] or "", country=r[6] or "") for r in cur.fetchall()]


def parse_ids(text):
    """'800-813,9' -> [9, 800, 801, ... 813]"""
    out = set()
    for part in str(text).split(","):
        a, _, b = part.strip().partition("-")
        if a.strip():
            out.update(range(int(a), int(b or a) + 1))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="Wikipedia stadium photos -> venues.photourl / photocredit")
    ap.add_argument("--lookup", metavar="NAME", help="search one venue name and print the result, then exit (no database)")
    ap.add_argument("--team", help="with --lookup: a team that plays there")
    ap.add_argument("--csv", help="read venues from this CSV instead of the database (use with --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="do not write to the database; write ./out/venue_photos.csv")
    ap.add_argument("--limit", type=int, default=150, help="venues to look up per run (default 150, 0 = all)")
    ap.add_argument("--national", action="store_true", help="only venues that host national-team competitions (league ids 800 and up)")
    ap.add_argument("--league-ids", help="only venues that host fixtures of these league ids, e.g. 800-813 or 9,12")
    ap.add_argument("--only", type=int, help="one venueid")
    ap.add_argument("--width", type=int, default=1280, choices=STEPS, help="thumbnail width to save (default 1280)")
    ap.add_argument("--replace-external", action="store_true", help="also look for Wikipedia photos for venues whose photo is on another site")
    ap.add_argument("--shrink", action="store_true", help="rewrite saved Wikimedia links wider than --width to --width (no search), then exit")
    ap.add_argument("--retry-days", type=int, default=60, help="retry venues with no match after this many days (default 60)")
    args = ap.parse_args()

    if args.lookup:
        r = find_photo(args.lookup, [args.team] if args.team else [], args.width)
        print(json.dumps(r, indent=2, ensure_ascii=False)); return

    url = os.environ.get("DATABASE_URL")
    if not url and not args.csv:
        sys.exit("Set DATABASE_URL (or use --csv with --dry-run, or --lookup NAME)")
    if args.csv and not args.dry_run:
        sys.exit("--csv only works together with --dry-run")

    if args.shrink:
        import psycopg
        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute("""SELECT venueid, photourl FROM "venues" WHERE photourl ~ '^https://(upload|thumb)\\.wikimedia\\.org/'""")
            todo = [(shrink_url(u, args.width), v) for v, u in cur.fetchall()]
            todo = [(n, v) for n, v in todo if n and len(n) <= 500]
            print(f"{len(todo)} photo link(s) wider than {args.width}px" + (" (dry run, nothing saved)" if args.dry_run else ""))
            if not args.dry_run:
                cur.executemany('UPDATE "venues" SET photourl = %s WHERE venueid = %s', todo)
        return

    teams = teams_by_venue()
    conn = None
    if args.csv:
        rows = [r for r in load_rows_csv(args.csv) if not r["photourl"] or (args.replace_external and not WIKIMEDIA.match(r["photourl"]))]
        rows = [r for r in rows if not args.only or r["venueid"] == args.only][:args.limit]
    else:
        import psycopg
        conn = psycopg.connect(url, autocommit=True)
        rows = db_rows(conn.cursor(), args.replace_external, args.retry_days, args.limit, args.only,
                       parse_ids(args.league_ids) if args.league_ids else None, 800 if args.national else None)
    print(f"{len(rows)} venue(s) to look up" + (" (dry run)" if args.dry_run else ""), flush=True)

    out, found, missed = [], 0, 0
    for v in rows:
        try:
            ctx = ([v["home"]] if v.get("home") else []) + [t for t in teams.get(v["venueid"], []) if t != v.get("home")]
            r = find_photo(v["fullname"], ctx[:3], args.width, v.get("city", ""), v.get("country", ""))
        except Exception as e:                              # noqa: BLE001 - one bad venue must not stop the run
            r = {"miss": f"error: {e}"}
        if r.get("url"):
            found += 1
            print(f'saved  {v["fullname"]} <- "{r["page"]}" ({r["file"]}) [{r.get("how", "wikipedia")}]\n       {r["url"]}\n       {r["credit"]}', flush=True)
            out.append([v["venueid"], v["fullname"], r["url"], r["credit"]])
            if conn and not args.dry_run:
                conn.execute('UPDATE "venues" SET photourl = %s, photocredit = %s WHERE venueid = %s', (r["url"], r["credit"], v["venueid"]))
                conn.execute("DELETE FROM venue_photo_misses WHERE venueid = %s", (v["venueid"],))
        else:
            missed += 1
            print(f'none   {v["fullname"]}: {r["miss"]}', flush=True)
            if conn and not args.dry_run:
                conn.execute("""INSERT INTO venue_photo_misses (venueid, reason) VALUES (%s, %s)
                                ON CONFLICT (venueid) DO UPDATE SET tried_at = now(), reason = EXCLUDED.reason""", (v["venueid"], r["miss"][:200]))
        time.sleep(0.4)                                     # be polite to Wikipedia
    if args.dry_run and out:
        os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
        with open(os.path.join(HERE, "out", "venue_photos.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL); w.writerow(["venueid", "fullname", "photourl", "photocredit"]); w.writerows(out)
        print("\nwrote out/venue_photos.csv")
    print(f"\n{found} photo(s) found, {missed} without a match.")
    if conn:
        conn.close()


if __name__ == "__main__":
    main()
