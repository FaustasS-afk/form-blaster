"""
retry_failed_countries.py — re-harvest only the countries that came back empty.

The first batch failed on Overpass rate-limiting (502/504), not bad queries:
firing 7 large country queries back-to-back at the same mirror got throttled.
Ireland and UAE worked because they were early/lucky.

Fix: one country per invocation with long pauses, and rotate mirrors on every
single attempt (not every 4th). Also shrinks each query by splitting the shop
tag union into two smaller passes, because a single giant union is what makes
Overpass 502.

    python retry_failed_countries.py fr
"""

import csv
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from find_leads_v2 import (OUT, UA, OVERPASS, COUNTRIES, WEBSITE_TAGS,
                           norm_site, BAD_HOST, FIELDS)

# Split the tag union so each Overpass request is small enough not to 502.
PASSES = [
    # pass A: explicit shop/craft values
    ('nwr["shop"="jewelry"](area.a);'
     'nwr["shop"="jewellery"](area.a);'
     'nwr["shop"="watches"](area.a);'
     'nwr["shop"="antiques"](area.a);'
     'nwr["shop"="antique"](area.a);'
     'nwr["shop"="second_hand"](area.a);'
     'nwr["shop"="pawnbroker"](area.a);'
     'nwr["shop"="gold_buyer"](area.a);'
     'nwr["craft"="jeweller"](area.a);'
     'nwr["craft"="watchmaker"](area.a);'
     'nwr["craft"="goldsmith"](area.a);'),
    # pass B: local-language NAME match (catches mistagged businesses)
    None,   # built from the country regex
]


def run_query(iso2, body, tries=6):
    q = ('[out:json][timeout:240];\n'
         'area["ISO3166-1"="%s"][admin_level=2]->.a;\n'
         '(\n%s\n);\nout tags center;' % (iso2, body))
    last = ""
    for a in range(tries):
        url = OVERPASS[a % len(OVERPASS)]      # rotate EVERY attempt
        try:
            r = httpx.post(url, data={"data": q}, headers=UA, timeout=300)
            if r.status_code == 200:
                j = r.json()
                n = len(j.get("elements", []))
                if n:
                    return j
                last = "200 but 0 elements"
            else:
                last = "http%d" % r.status_code
        except Exception as e:
            last = type(e).__name__
        wait = 15 + a * 15
        print("      retry %d (%s), sleep %ds" % (a + 1, last, wait), flush=True)
        time.sleep(wait)
    print("      GAVE UP: %s" % last)
    return None


def rows_from(j, iso):
    rows, seen = [], set()
    for e in j.get("elements", []):
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
            "country": iso.upper(), "domain": dom, "site": site,
            "name": (t.get("name") or "").strip(),
            "phone": (t.get("phone") or t.get("contact:phone") or "").strip(),
            "category": (t.get("shop") or t.get("craft") or "").strip(),
            "city": t.get("addr:city", ""),
            "postcode": t.get("addr:postcode", ""),
            "street": " ".join(x for x in [t.get("addr:housenumber", ""),
                                           t.get("addr:street", "")] if x),
            "source": "osm_%s/%s" % (e.get("type"), e.get("id")),
        })
    return rows


def main():
    targets = sys.argv[1:] or ["ca", "fr", "it", "es", "us"]
    grand = 0
    for iso in targets:
        if iso not in COUNTRIES:
            continue
        iso2, name_re = COUNTRIES[iso]
        print("\n=== %s ===" % iso.upper(), flush=True)
        rows, seen = [], set()

        bodies = [PASSES[0],
                  'nwr["name"~"%s",i]["shop"](area.a);'
                  'nwr["name"~"%s",i]["craft"](area.a);' % (name_re, name_re)]
        for i, body in enumerate(bodies, 1):
            print("   pass %d/%d ..." % (i, len(bodies)), flush=True)
            j = run_query(iso2, body)
            if not j:
                continue
            got = rows_from(j, iso)
            for r in got:
                if r["domain"] in seen:
                    continue
                seen.add(r["domain"])
                rows.append(r)
            print("      +%d (total %d)" % (len(got), len(rows)), flush=True)
            time.sleep(12)

        if rows:
            p = OUT / ("leads_%s.csv" % iso)
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            print("   -> %s  (%d)" % (p.name, len(rows)), flush=True)
            grand += len(rows)

    # rebuild combined
    all_rows, seen = [], set()
    for iso in COUNTRIES:
        p = OUT / ("leads_%s.csv" % iso)
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["domain"] in seen:
                    continue
                seen.add(r["domain"])
                all_rows.append(r)

    with open(OUT / "leads_world_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    (OUT / "leads_world_all.txt").write_text(
        "\n".join(sorted(r["site"] for r in all_rows if r["site"])) + "\n",
        encoding="utf-8")

    import collections
    print("\n" + "=" * 58)
    print("  COMBINED TOTAL: %d" % len(all_rows))
    for k, v in collections.Counter(r["country"] for r in all_rows).most_common():
        print("    %s: %d" % (k, v))
    print("=" * 58)


if __name__ == "__main__":
    main()
