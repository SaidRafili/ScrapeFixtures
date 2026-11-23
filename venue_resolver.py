#!/usr/bin/env python3
"""
venue_resolver.py - find a free photo of a stadium WITHOUT depending on the English Wikipedia article title.

Why the old finder missed national-team grounds: it only searched en.wikipedia for "<name> stadium". ESPN names often differ from Wikipedia
("Olympic Stadium Baku" vs "Baku Olympic Stadium", "Tofiq Bahramov adına Respublika Stadionu", "Stade de France" vs "Stade de France (Saint-Denis)").

This module resolves the venue in three independent steps and uses the first that yields a FREE picture:

  1. Wikidata entity search (any language, aliases included) -> must be a stadium/arena class, country/city must agree with ESPN's city/country.
     Picture = P18 (image), else P4291 (panorama), else a file from the Commons category (P373).
  2. Commons GEO-search around the entity's coordinates (P625): free photos taken within 250 m, file name must look like a stadium photo.
  3. Commons full-text search "<name> <city>" in the File: namespace.

Every candidate goes through commons_file(): free licence only (no NonFree flag, no NC/ND), jpeg/png/webp, >= 500 px wide, returns Wikimedia's own
thumbnail URL (default 1280 px, never the multi-MB original) and a credit line.

Pure helpers have no network access, so they can be unit tested. Network calls reuse `api()` from sync_venue_photos.
"""
import re
from sync_venue_photos import (api, commons_file, distinctive, is_photo_file, tokens, clean_name)

# Wikidata "instance of" classes that mean "a place where football is played"
STADIUM_QIDS = {"Q483110",     # stadium
                "Q1154710",    # football stadium
                "Q641226",     # arena
                "Q1076486",    # sports venue
                "Q1065118",    # multi-purpose stadium
                "Q18567958",   # national stadium
                "Q13226383",   # athletics facility
                }
DESC_HINT = re.compile(r"stadium|stadion|stade|estadio|stadio|arena|football ground|sports venue|velodrome|coliseum|football pitch", re.I)
NC_ND = re.compile(r"(?:^|[ -])(NC|ND)(?:[ -]|$)", re.I)                    # CC BY-NC, CC BY-ND ... not allowed on a commercial product
GEO_RADIUS_M = 250


# ------------------------------------------------------------------ pure helpers
def commons_title(filename):
    return filename if filename.lower().startswith("file:") else "File:" + filename.replace(" ", "_")


def wd_claim_values(entity, prop):
    out = []
    for c in (entity.get("claims") or {}).get(prop, []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if v is not None:
            out.append(v)
    return out


def wd_instance_ids(entity):
    return {v.get("id") for v in wd_claim_values(entity, "P31") if isinstance(v, dict)}


def wd_is_stadium(entity):
    if wd_instance_ids(entity) & STADIUM_QIDS:
        return True
    desc = " ".join(d.get("value", "") for d in (entity.get("descriptions") or {}).values())
    return bool(DESC_HINT.search(desc))


def wd_labels(entity):
    return [l.get("value", "") for l in (entity.get("labels") or {}).values()] + \
           [a.get("value", "") for al in (entity.get("aliases") or {}).values() for a in al]


def name_score(name, entity):
    """0..1: share of the venue's distinctive words found in the best label/alias of the entity."""
    want = distinctive(name)
    if not want:
        return 0.0
    best = 0.0
    for label in wd_labels(entity):
        have = set(tokens(label))
        best = max(best, sum(1 for t in want if t in have) / len(want))
    return best


def free_licence(credit):
    """credit line from sync_venue_photos.credit_line ('Author / Wikimedia Commons, CC BY-SA 4.0') -> False for NC / ND licences."""
    lic = credit.rsplit(",", 1)[-1] if "," in credit else ""
    return not NC_ND.search(lic)


def coordinates(entity):
    for v in wd_claim_values(entity, "P625"):
        if isinstance(v, dict) and "latitude" in v:
            return v["latitude"], v["longitude"]
    return None


# ------------------------------------------------------------------ network
def wd_search(name, lang="en", limit=7):
    j = api("www.wikidata.org", action="wbsearchentities", search=name, language=lang, uselang=lang, type="item", limit=limit)
    return [r["id"] for r in j.get("search", [])]


def wd_entities(ids):
    if not ids:
        return {}
    j = api("www.wikidata.org", action="wbgetentities", ids="|".join(ids[:20]), props="claims|labels|aliases|descriptions|sitelinks", languages="en|fr|de|es|it|tr|az|ru|pt|ar")
    return j.get("entities") or {}


def country_ok(entity, country, city):
    """Reject a same-named stadium elsewhere. Only the Wikidata DESCRIPTION is judged ("stadium in Berlin, Germany"): it must mention ESPN's
    city or country, or else not name any place at all. No country/city from ESPN, or no description -> nothing to contradict."""
    if not (country or city):
        return True
    descs = [d.get("value", "") for d in (entity.get("descriptions") or {}).values()]
    desc = " ".join(descs).lower()
    if not desc.strip():
        return True
    if any(w.lower() in desc for w in (country, city) if w):
        return True
    return not re.search(r"\bin [A-Z][\w'-]+", " ".join(descs))                # names another place -> reject


def geo_files(lat, lon, width):
    j = api("commons.wikimedia.org", action="query", list="geosearch", gscoord=f"{lat}|{lon}", gsradius=GEO_RADIUS_M, gsnamespace=6, gslimit=30)
    names = [g["title"].split(":", 1)[1].replace(" ", "_") for g in (j.get("query") or {}).get("geosearch", [])]
    return [n for n in names if is_photo_file(n)]


def category_files(cat, width, limit=15):
    j = api("commons.wikimedia.org", action="query", list="categorymembers", cmtitle="Category:" + cat, cmtype="file", cmlimit=limit)
    names = [m["title"].split(":", 1)[1].replace(" ", "_") for m in (j.get("query") or {}).get("categorymembers", [])]
    return [n for n in names if is_photo_file(n)]


def search_files(query, limit=8):
    j = api("commons.wikimedia.org", action="query", list="search", srsearch=query, srnamespace=6, srlimit=limit)
    names = [s["title"].split(":", 1)[1].replace(" ", "_") for s in (j.get("query") or {}).get("search", [])]
    return [n for n in names if is_photo_file(n)]


def first_free(filenames, width):
    for fn in filenames:
        got = commons_file(fn, width)
        if got and free_licence(got["credit"]):
            return got
    return None


def resolve(name, city="", country="", teams=(), width=1280):
    """-> {url, credit, file, page, how} or {miss: reason}"""
    name = clean_name(name)
    queries = list(dict.fromkeys([q for q in (name, f"{name} {city}".strip()) if q]))
    cand_ids = []
    for q in queries:
        for lang in ("en", "local"):
            try:
                cand_ids += wd_search(q, "en" if lang == "en" else "mul")
            except Exception:                               # noqa: BLE001 - 'mul' not available everywhere
                pass
    cand_ids = list(dict.fromkeys(cand_ids))[:20]
    ents = wd_entities(cand_ids)
    ranked = []
    for qid in cand_ids:
        e = ents.get(qid) or {}
        if not e or not wd_is_stadium(e) or not country_ok(e, country, city):
            continue
        s = name_score(name, e)
        if s >= 0.6:
            ranked.append((s, qid, e))
    ranked.sort(key=lambda t: -t[0])

    for s, qid, e in ranked[:3]:
        files = [v for p in ("P18", "P4291") for v in wd_claim_values(e, p) if isinstance(v, str)]
        got = first_free([f.replace(" ", "_") for f in files], width)
        if got:
            return {**got, "page": qid, "how": "wikidata image"}
        cats = [v for v in wd_claim_values(e, "P373") if isinstance(v, str)]
        for cat in cats[:1]:
            got = first_free(category_files(cat, width), width)
            if got:
                return {**got, "page": qid, "how": "commons category"}
        xy = coordinates(e)
        if xy:
            got = first_free(geo_files(xy[0], xy[1], width)[:10], width)
            if got:
                return {**got, "page": qid, "how": "commons geosearch"}

    for q in queries:
        got = first_free(search_files(f"{q} stadium")[:8], width)
        if got and set(distinctive(name)) & set(tokens(got["file"])):
            return {**got, "page": "", "how": "commons text search"}
    return {"miss": "no free photo found (wikidata, geosearch, commons search)"}
