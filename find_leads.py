"""
find_leads.py — multi-country second-hand jewellery / watch lead harvester.

Free sources only (Apify has ~$0.013 left, which is nothing).

Sources, in order of reliability:
  1. localvintagestores.com   US only, but huge and already proven (~9.9k)
  2. antiquestoreatlas.com    US only, jewellery slice
  3. Google Places via the FREE "Places API (New)" is dead here, so:
  4. OpenStreetMap Overpass   ALL countries, free, no key, no quota
     -> shop=jewelry / shop=watches / shop=antique across CA IE AE FR IT ES US

Overpass is the workhorse for the 7 countries. It returns websites directly via
tags (website / contact:website), which is exactly the outbound link needed.

Writes one file per country plus a combined file. Resumable per source.
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
OUT = BASE / "leads_world"
OUT.mkdir(exist_ok=True)

H = {"User-Agent": "EasyListingsLeads/1.0 (contact info@easylistings.co.uk)"}

# ── Overpass: country ISO -> list of area names ───────────────────────────────
COUNTRIES = {
    "us": ["United States"],
    "ca": ["Canada"],
    "ie": ["Ireland"],
    "ae": ["United Arab Emirates"],
    "fr": ["France"],
    "it": ["Italy"],
    "es": ["Spain"],
}

# shop tags that mean second-hand jewellery / watch / antique dealer
SHOP_TAGS = [
    "jewelry", "jewellery", "watches", "antiques", "antique",
    "gold_buyer", "pawnbroker", "second_hand", "vintage", "collector",
]

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

WEBSITE_TAGS = [
    "website", "contact:website", "url", "website:en",
    "contact:url", "brand:website",
]

TAG_STRIP = re.compile(r"^(https?://)?(www\.)?", re.I)


def _norm_website(v):
    if not v:
        return ""
    v = v.strip().rstrip("/")
    if v.startswith("//"):
        v = "https:" + v
    elif not v.startswith("http"):
        v = "https://" + v
    return v


def build_query(area_names, shop_tags):
    """Overpass QL: businesses in these areas with any shop tag we want."""
    area_clauses = []
    for nm in area_names:
        area_clauses.append(f'area["name"="{nm}"]["admin_level"~"^(2|4)$"]->.a;')
    shop_re = "|".join(shop_tags)
    parts = []
    for a in area_clauses:
        parts.append(a)
        parts.append(f"""
(
  nwr["shop"~"^({shop_re})$"](area.a);
  nwr["shop"="jewelry"]["second_hand"="yes"](area.a);
  nwr["craft"="jeweller"](area.a);
  nwr["craft"="watchmaker"](area.a);
  nwr["shop"="antiques"](area.a);
);
""")
    q = "[out:json][timeout:180];\n" + "\n".join(parts) + """
out tags center;"""
    return q


def overpass(q, tries=3):
    last = ""
    for attempt in range(tries):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            r = httpx.post(url, data={"data": q}, headers=H, timeout=240)
            if r.status_code == 200:
                return r.json()
            last = f"http{r.status_code} {r.text[:120]}"
            # 429/504 -> back off and try the next mirror
            time.sleep(5 + attempt * 10)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(4 + attempt * 6)
    print(f"    overpass failed: {last}")
    return None


def harvest_country(iso, names):
    print(f"\n=== {iso.upper()}  {names} ===")
    q = build_query(names, SHOP_TAGS)
    j = overpass(q)
    if not j:
        return []
    els = j.get("elements", [])
    print(f"    overpass returned {len(els)} elements")

    out = []
    seen = set()
    for e in els:
        t = e.get("tags") or {}
        site = ""
        for wt in WEBSITE_TAGS:
            if t.get(wt):
                site = _norm_website(t[wt])
                break
        if not site:
            continue
        dom = TAG_STRIP.sub("", site).split("/")[0].lower()
        if not dom or "." not in dom:
            continue
        # drop obvious non-business / social / directory hosts
        if any(s in dom for s in ("facebook.", "instagram.", "twitter.",
                                  "google.", "yelp.", "tripadvisor.")):
            continue
        if dom in seen:
            continue
        seen.add(dom)

        addr = " ".join(x for x in [
            t.get("addr:housenumber", ""), t.get("addr:street", ""),
            t.get("addr:city", ""), t.get("addr:postcode", ""),
        ] if x)

        out.append({
            "country": iso.upper(),
            "domain": dom,
            "site": site,
            "name": (t.get("name") or "").strip(),
            "phone": (t.get("phone") or t.get("contact:phone") or "").strip(),
            "category": (t.get("shop") or t.get("craft") or "").strip(),
            "city": t.get("addr:city", ""),
            "address": addr,
            "source": f"osm:{e.get('type')}/{e.get('id')}",
            "second_hand": t.get("second_hand", ""),
        })
    print(f"    {len(out)} unique business websites")
    return out


def main():
    only = sys.argv[1:] or list(COUNTRIES)
    all_rows = []

    for iso in only:
        names = COUNTRIES.get(iso)
        if not names:
            continue
        rows = harvest_country(iso, names)
        if rows:
            p = OUT / f"leads_{iso}.csv"
            import csv
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            print(f"    -> {p.name}")
        all_rows += rows

    # combined, deduped by domain
    seen = set()
    combo = []
    for r in all_rows:
        if r["domain"] in seen:
            continue
        seen.add(r["domain"])
        combo.append(r)

    import csv
    cp = OUT / "leads_world_all.csv"
    if combo:
        with open(cp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(combo[0].keys()))
            w.writeheader()
            w.writerows(combo)
    tp = OUT / "leads_world_all.txt"
    tp.write_text(
        "\n".join(sorted(r["site"] for r in combo if r["site"])) + "\n",
        encoding="utf-8")

    print()
    print("=" * 60)
    print(f"  TOTAL unique international leads: {len(combo)}")
    import collections
    for k, v in collections.Counter(r["country"] for r in combo).most_common():
        print(f"    {k}: {v}")
    print(f"  {cp}")
    print(f"  {tp}")
    print("=" * 60)


if __name__ == "__main__":
    main()
