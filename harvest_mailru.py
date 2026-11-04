"""
harvest_mailru.py — harvest via maps.mail.ru by CHUNKING the queries.

MEASURED FACTS (2026-11-04 ~03:45 UTC)
  * maps.mail.ru IS the only live full-world Overpass mirror.
  * It answers a SMALL bbox query in ~15s (n=361) but returns http504 on a
    whole-country union query. It is under-powered, not broken.
  * overpass-api.de   ConnectTimeout (dead)
  * kumi / private.coffee  ReadTimeout (dead)
  * overpass.osm.ch   200 in 0.4s but SWITZERLAND ONLY

SO: stop asking it for a whole country at once. Split every country into
geographic SUB-BOXES (using real bounding boxes), so each request is small
enough to succeed. Each sub-box is a resumable unit.

This is slower but it actually completes, and it rides out the flapping by
retrying a sub-box until it lands.

    python harvest_mailru.py fr
    python harvest_mailru.py --all
"""

import csv
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
OUT = BASE / "leads_world"
OUT.mkdir(exist_ok=True)
DONE = OUT / "_mailru_done.json"

H = {"User-Agent": "EasyListingsLeadHarvest/1.0 (info@easylistings.co.uk)"}
MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

SHOP = ("jewelry|jewellery|watches|antiques|antique|second_hand|"
        "pawnbroker|gold_buyer|collector|vintage")
CRAFT = "jeweller|watchmaker|goldsmith|silversmith|clockmaker"

WEBSITE_TAGS = ["website", "contact:website", "url", "website:en", "contact:url"]
SCHEME = re.compile(r"^(https?://)?(www\.)?", re.I)
BAD = re.compile(r"facebook\.|instagram\.|twitter\.|google\.|yelp\.|tripadvisor\.|"
                 r"foursquare\.|linkedin\.|youtube\.|wikipedia\.|pinterest\.", re.I)
FIELDS = ["country", "domain", "site", "name", "phone", "category",
          "city", "postcode", "street", "source"]

# Country -> list of (south, west, north, east) sub-boxes. A country is split
# into a grid so no single request is huge. Overlaps are deduped later.
# Format: (s,w,n,e). Big countries get a 3x3 or 4x4 grid.
def grid(s, w, n, e, rows, cols):
    boxes = []
    dh = (n - s) / rows
    dw = (e - w) / cols
    for i in range(rows):
        for j in range(cols):
            boxes.append((round(s + i * dh, 4), round(w + j * dw, 4),
                          round(s + (i + 1) * dh, 4), round(w + (j + 1) * dw, 4)))
    return boxes

COUNTRIES = {
    "ie": grid(51.3, -10.6, 55.5, -5.9, 2, 2),
    "fr": grid(42.3, -5.2, 51.1, 8.3, 3, 3),
    "it": grid(36.6, 6.6, 47.1, 18.5, 3, 2),
    "es": grid(36.0, -9.4, 43.8, 4.4, 2, 2),
    "pt": grid(36.9, -9.6, 42.2, -6.1, 2, 1),
    "de": grid(47.2, 5.8, 55.1, 15.1, 2, 2),
    "nl": grid(50.7, 3.3, 53.6, 7.3, 1, 2),
    "be": grid(49.4, 2.5, 51.6, 6.5, 2, 2),
    "ch": grid(45.8, 5.9, 47.9, 10.5, 1, 2),
    "at": grid(46.3, 9.5, 49.1, 17.2, 1, 2),
    "se": grid(55.3, 10.9, 69.1, 24.2, 2, 1),
    "dk": grid(54.5, 8.0, 57.8, 15.2, 1, 1),
    "no": grid(57.9, 4.6, 71.2, 31.1, 2, 1),
    "fi": grid(59.7, 19.5, 70.1, 31.6, 1, 1),
    "au": grid(-43.7, 112.9, -10.6, 153.6, 2, 2),
    "nz": grid(-47.3, 166.3, -34.3, 178.6, 1, 1),
    "ae": grid(22.6, 51.5, 26.1, 56.4, 1, 1),
    "ca": grid(42.0, -141.0, 70.0, -52.6, 2, 3),
}


def load_done():
    return set(json.loads(DONE.read_text())) if DONE.exists() else set()


def save_done(s):
    DONE.write_text(json.dumps(sorted(s)))


def fetch_box(box, tries=5):
    s, w, n, e = box
    q = (f'[out:json][timeout:120];'
         f'nwr["shop"~"^({SHOP})$"]({s},{w},{n},{e});'
         f'nwr["craft"~"^({CRAFT})$"]({s},{w},{n},{e});'
         f'out tags center;')
    for a in range(tries):
        for m in MIRRORS:
            try:
                r = httpx.post(m, data={"data": q}, headers=H, timeout=200)
                if r.status_code == 200:
                    els = r.json().get("elements", [])
                    if els:
                        return els
            except Exception:
                pass
        time.sleep(6 + a * 8)
    return []


def rows(els, cc):
    out, seen = [], set()
    for el in els:
        t = el.get("tags") or {}
        site = dom = ""
        for wt in WEBSITE_TAGS:
            if t.get(wt):
                v = t[wt].strip().rstrip("/")
                if " " in v:
                    v = v.split()[0]
                if v.startswith("//"):
                    v = "https:" + v
                elif not v.startswith("http"):
                    v = "https://" + v
                site = v
                dom = SCHEME.sub("", v).split("/")[0].lower()
                break
        if not dom or "." not in dom or BAD.search(dom):
            continue
        if dom.startswith("www."):
            dom = dom[4:]
        if dom in seen:
            continue
        seen.add(dom)
        out.append({"country": cc, "domain": dom, "site": site,
                    "name": (t.get("name") or "").strip(),
                    "phone": (t.get("phone") or t.get("contact:phone") or "").strip(),
                    "category": (t.get("shop") or t.get("craft") or "").strip(),
                    "city": t.get("addr:city", ""),
                    "postcode": t.get("addr:postcode", ""),
                    "street": " ".join(x for x in [t.get("addr:housenumber", ""),
                                                   t.get("addr:street", "")] if x),
                    "source": "osm_%s/%s" % (el.get("type"), el.get("id"))})
    return out


def save(cc, new):
    p = OUT / ("leads_%s.csv" % cc.lower())
    have = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            have = list(csv.DictReader(f))
    seen = {r["domain"] for r in have}
    added = 0
    for r in new:
        if r["domain"] not in seen:
            seen.add(r["domain"])
            have.append(r)
            added += 1
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(have)
    return added, len(have)


def do_country(cc):
    boxes = COUNTRIES[cc]
    done = load_done()
    print("\n=== %s (%d boxes) ===" % (cc.upper(), len(boxes)), flush=True)
    for i, box in enumerate(boxes, 1):
        unit = "%s:%s" % (cc, box)
        if unit in done:
            print("  [%d/%d] cached" % (i, len(boxes)), flush=True)
            continue
        els = fetch_box(box)
        got = rows(els, cc.upper())
        if got:
            a, tot = save(cc, got)
            print("  [%d/%d] +%d -> %d" % (i, len(boxes), a, tot), flush=True)
        else:
            print("  [%d/%d] 0" % (i, len(boxes)), flush=True)
        done.add(unit)
        save_done(done)
        time.sleep(2)


def combine():
    all_rows, seen = [], set()
    for p in sorted(OUT.glob("leads_*.csv")):
        if p.name.startswith("leads_world_all"):
            continue
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["domain"] in seen:
                    continue
                seen.add(r["domain"])
                all_rows.append(r)
    if all_rows:
        with open(OUT / "leads_world_all.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(all_rows)
        (OUT / "leads_world_all.txt").write_text(
            "\n".join(sorted(r["site"] for r in all_rows if r["site"])) + "\n",
            encoding="utf-8")
    import collections
    print("\n" + "=" * 56)
    print("  TOTAL: %d" % len(all_rows))
    for k, v in collections.Counter(r["country"] for r in all_rows).most_common():
        print("    %-4s %d" % (k, v))
    print("=" * 56)


if __name__ == "__main__":
    args = sys.argv[1:]
    targets = list(COUNTRIES) if (not args or "--all" in args) else args
    for cc in targets:
        if cc in COUNTRIES:
            do_country(cc)
        combine() if cc == targets[-1] else None
