"""
find_leads.py — multi-country second-hand jewellery / watch / antique harvester.

100% FREE. No Apify (account has ~$0.013). Uses OpenStreetMap Overpass:
no API key, no quota, returns the business's own website tag directly.

    python find_leads.py                # every country
    python find_leads.py ie fr it es    # just these

Writes leads_world/leads_<iso>.csv per country plus a combined dedupe.
Resumable: a country already harvested is skipped unless --force.

TAG STRATEGY (this is what makes or breaks the yield)
Overpass only returns businesses whose OWN website is tagged, so the yield is
~30% of elements. Breadth comes from querying many shop/craft values AND the
local-language name patterns per country, because OSM tagging is inconsistent:
"Bijouterie" in France, "Gioielleria" in Italy, "Joyeria" in Spain are usually
tagged shop=jewelry but the NAME is the reliable local-language signal.
"""

import argparse
import collections
import csv
import re
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
OUT = BASE / "leads_world"
OUT.mkdir(exist_ok=True)

UA = {"User-Agent": "EasyListingsLeadHarvest/1.0 (info@easylistings.co.uk)"}

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# ISO -> (ISO3166 area code, local-language name regex)
COUNTRIES = {
    "us": ("US", r"jewel|antique|vintage|pawn|watch|gold|silver|estate|consign"),
    "ca": ("CA", r"jewel|antique|vintage|pawn|watch|gold|silver|estate|consign"),
    "ie": ("IE", r"jewel|antique|vintage|watch|gold|silver|pawn"),
    "ae": ("AE", r"jewel|gold|watch|antique|silver|diamond"),
    "fr": ("FR", r"bijou|joaill|horlog|antiqu|brocante|orfev|montre|"
                 r"argenterie|diamant"),
    "it": ("IT", r"gioiell|orolog|antiqu|orefic|argent|diamant"),
    "es": ("ES", r"joyer|reloj|antig|plata|segunda mano|ocasi|diamant|almoneda"),
}

WEBSITE_TAGS = ["website", "contact:website", "url", "website:en", "contact:url"]

SHOP_VALUES = [
    "jewelry", "jewellery", "watches", "antiques", "antique",
    "second_hand", "pawnbroker", "gold_buyer", "collector", "vintage",
    "charity", "clothes", "fashion_accessories", "bag",
]
CRAFT_VALUES = ["jeweller", "watchmaker", "goldsmith", "silversmith", "clockmaker"]

SCHEME = re.compile(r"^(https?://)?(www\.)?", re.I)
BAD_HOST = re.compile(
    r"facebook\.|instagram\.|twitter\.|google\.|yelp\.|tripadvisor\.|"
    r"foursquare\.|linkedin\.|youtube\.|wikipedia\.|pinterest\.", re.I)


def build_query(iso2, name_re):
    shop_re = "|".join(SHOP_VALUES)
    craft_re = "|".join(CRAFT_VALUES)
    return (
        '[out:json][timeout:180];\n'
        f'area["ISO3166-1"="{iso2}"][admin_level=2]->.a;\n'
        '(\n'
        f'  nwr["shop"~"^({shop_re})$"](area.a);\n'
        f'  nwr["craft"~"^({craft_re})$"](area.a);\n'
        f'  nwr["name"~"{name_re}",i]["shop"](area.a);\n'
        f'  nwr["name"~"{name_re}",i]["craft"](area.a);\n'
        ');\n'
        'out tags center;'
    )


def overpass(q, tries=4):
    last = ""
    for attempt in range(tries):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            r = httpx.post(url, data={"data": q}, headers=UA, timeout=300)
            if r.status_code == 200:
                return r.json()
            last = "http%d" % r.status_code
            time.sleep(8 + attempt * 12)
        except Exception as e:
            last = type(e).__name__
            time.sleep(6 + attempt * 10)
    print("    overpass FAILED (%s)" % last)
    return None


def norm_site(v):
    v = (v or "").strip().rstrip("/")
    if not v:
        return "", ""
    if " " in v:
        v = v.split()[0]
    if v.startswith("//"):
        v = "https:" + v
    elif not v.startswith("http"):
        v = "https://" + v
    dom = SCHEME.sub("", v).split("/")[0].lower()
    return v, dom


def harvest(iso, iso2, name_re):
    print("\n=== %s ===" % iso.upper(), flush=True)
    j = overpass(build_query(iso2, name_re))
    if not j:
        return []
    els = j.get("elements", [])
    print("    %d OSM elements" % len(els), flush=True)

    rows, seen = [], set()
    for e in els:
        t = e.get("tags") or {}
        site = dom = ""
        for wt in WEBSITE_TAGS:
            if t.get(wt):
                site, dom = norm_site(t[wt])
                break
        if not dom or "." not in dom or BAD_HOST.search(dom):
            continue
        if dom.startswith("www."):
            dom = dom[4:]
        if dom in seen:
            continue
        seen.add(dom)
        rows.append({
            "country": iso.upper(),
            "domain": dom,
            "site": site,
            "name": (t.get("name") or "").strip(),
            "phone": (t.get("phone") or t.get("contact:phone") or "").strip(),
            "category": (t.get("shop") or t.get("craft") or "").strip(),
            "city": t.get("addr:city", ""),
            "postcode": t.get("addr:postcode", ""),
            "street": " ".join(x for x in [t.get("addr:housenumber", ""),
                                           t.get("addr:street", "")] if x),
            "source": "osm_%s/%s" % (e.get("type"), e.get("id")),
        })
    print("    %d unique business websites" % len(rows), flush=True)
    return rows


FIELDS = ["country", "domain", "site", "name", "phone", "category",
          "city", "postcode", "street", "source"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("countries", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    todo = args.countries or list(COUNTRIES)
    all_rows = []

    for iso in todo:
        if iso not in COUNTRIES:
            continue
        p = OUT / ("leads_%s.csv" % iso)
        if p.exists() and not args.force:
            with open(p, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            print("\n=== %s (cached %d) ===" % (iso.upper(), len(rows)))
            all_rows += rows
            continue
        iso2, name_re = COUNTRIES[iso]
        rows = harvest(iso, iso2, name_re)
        if rows:
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            print("    -> %s" % p.name)
        all_rows += rows

    seen, combo = set(), []
    for r in all_rows:
        if r["domain"] in seen:
            continue
        seen.add(r["domain"])
        combo.append(r)

    if combo:
        with open(OUT / "leads_world_all.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(combo)
        (OUT / "leads_world_all.txt").write_text(
            "\n".join(sorted(r["site"] for r in combo if r["site"])) + "\n",
            encoding="utf-8")

    print()
    print("=" * 62)
    print("  TOTAL international leads: %d" % len(combo))
    for k, v in collections.Counter(r["country"] for r in combo).most_common():
        print("    %s: %d" % (k, v))
    print("  -> %s" % (OUT / "leads_world_all.csv"))
    print("=" * 62)


if __name__ == "__main__":
    main()
