"""
harvest_us_states.py + robust country harvest, one mirror only.

Lessons from the failures:
  * overpass.kumi.systems and overpass.openstreetmap.ru are currently DEAD
    (ReadTimeout / ConnectTimeout). Retrying them wastes the whole budget.
  * overpass-api.de IS alive and returns data. Use it EXCLUSIVELY.
  * A 7-country union in one process rate-limits -> do one query at a time.
  * "200 but 0 elements" means throttled-then-empty, not no-data. Re-query.
  * The US nationwide query times out; per-STATE areas work fine (TX = 174).

So: single mirror, small queries, long sleeps, resumable per unit.

    python harvest_robust.py ie fr it es ae ca
    python harvest_robust.py --us
"""

import csv
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
OUT = BASE / "leads_world"
OUT.mkdir(exist_ok=True)

MIRROR = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "EasyListingsLeadHarvest/1.0 (info@easylistings.co.uk)"}

COUNTRIES = {
    "ie": ("IE", r"jewel|antique|vintage|watch|gold|silver|pawn"),
    "fr": ("FR", r"bijou|joaill|horlog|antiqu|brocante|orfev|montre|argenterie|diamant"),
    "it": ("IT", r"gioiell|orolog|antiqu|orefic|argent|diamant"),
    "es": ("ES", r"joyer|reloj|antig|plata|segunda mano|ocasi|diamant"),
    "ae": ("AE", r"jewel|gold|watch|antique|silver|diamond"),
    "ca": ("CA", r"jewel|antique|vintage|pawn|watch|gold|silver"),
    "pt": ("PT", r"joalh|relog|antig|ouriv|prata|diamant"),
    "de": ("DE", r"schmuck|juwel|uhren|antik|goldschmied|silber|gebraucht"),
    "nl": ("NL", r"juwel|sieraad|horloge|antiek|goud|zilver|tweedehands"),
    "be": ("BE", r"bijou|juwel|horloge|antiek|goud|zilver"),
    "ch": ("CH", r"bijou|schmuck|uhren|antik|orfevre|juwel"),
    "at": ("AT", r"schmuck|juwel|uhren|antik|goldschmied"),
    "se": ("SE", r"smycke|klock|antik|guld|silver"),
    "dk": ("DK", r"smykke|ur|antik|guld|solver"),
    "au": ("AU", r"jewel|antique|vintage|watch|gold|silver|pawn"),
    "nz": ("NZ", r"jewel|antique|vintage|watch|gold|silver"),
}

SHOP_RE = ("jewelry|jewellery|watches|antiques|antique|second_hand|"
           "pawnbroker|gold_buyer|collector|vintage")
CRAFT_RE = "jeweller|watchmaker|goldsmith|silversmith|clockmaker"

WEBSITE_TAGS = ["website", "contact:website", "url", "website:en", "contact:url"]

import re
SCHEME = re.compile(r"^(https?://)?(www\.)?", re.I)
BAD = re.compile(r"facebook\.|instagram\.|twitter\.|google\.|yelp\.|tripadvisor\.|"
                 r"foursquare\.|linkedin\.|youtube\.|wikipedia\.|pinterest\.", re.I)
FIELDS = ["country", "domain", "site", "name", "phone", "category",
          "city", "postcode", "street", "source"]

# US states: use ISO3166-2 area codes
US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL",
             "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT",
             "NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI",
             "SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"]


def q_for_area(area_sel, name_re=None):
    body = (f'  nwr["shop"~"^({SHOP_RE})$"](area.a);\n'
            f'  nwr["craft"~"^({CRAFT_RE})$"](area.a);\n')
    if name_re:
        body += (f'  nwr["name"~"{name_re}",i]["shop"](area.a);\n'
                 f'  nwr["name"~"{name_re}",i]["craft"](area.a);\n')
    return ('[out:json][timeout:180];\n'
            f'{area_sel}->.a;\n(\n{body});\nout tags center;')


def run(q, tries=5):
    for a in range(tries):
        try:
            r = httpx.post(MIRROR, data={"data": q}, headers=UA, timeout=240)
            if r.status_code == 200:
                els = r.json().get("elements", [])
                if els:
                    return els
                print("      empty(200) retry", flush=True)
            else:
                print("      http%d retry" % r.status_code, flush=True)
        except Exception as e:
            print("      %s retry" % type(e).__name__, flush=True)
        time.sleep(10 + a * 8)
    return []


def rows(els, cc):
    out, seen = [], set()
    for e in els:
        t = e.get("tags") or {}
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
                    "source": "osm_%s/%s" % (e.get("type"), e.get("id"))})
    return out


def save(cc, all_rows):
    p = OUT / ("leads_%s.csv" % cc.lower())
    existing = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    seen = {r["domain"] for r in existing}
    for r in all_rows:
        if r["domain"] not in seen:
            seen.add(r["domain"])
            existing.append(r)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing)
    print("   saved %s -> %d" % (p.name, len(existing)), flush=True)


def do_country(iso):
    area, name_re = COUNTRIES[iso]
    print("\n=== %s ===" % iso.upper(), flush=True)
    acc = []
    for i, name in enumerate([None, name_re], 1):
        print("   pass %d/2" % i, flush=True)
        els = run(q_for_area(f'area["ISO3166-1"="{area}"][admin_level=2]', name))
        got = rows(els, iso.upper())
        acc += got
        print("      +%d (total %d)" % (len(got), len(acc)), flush=True)
        time.sleep(8)
    if acc:
        save(iso, acc)
    return len(acc)


def do_us():
    print("\n=== US by state (%d) ===" % len(US_STATES), flush=True)
    for i, st in enumerate(US_STATES, 1):
        p = OUT / "leads_us.csv"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                have = {r["domain"] for r in csv.DictReader(f)}
        else:
            have = set()
        print("  [%d/%d] US-%s" % (i, len(US_STATES), st), flush=True)
        els = run(q_for_area(f'area["ISO3166-2"="US-{st}"][admin_level=4]'))
        got = [r for r in rows(els, "US") if r["domain"] not in have]
        if got:
            save("us", got)
        time.sleep(6)


def main():
    args = sys.argv[1:]
    if "--us" in args:
        do_us()
        return
    for iso in (args or list(COUNTRIES)):
        if iso in COUNTRIES:
            do_country(iso)
    combine()


def combine():
    all_rows, seen = [], set()
    for p in sorted(OUT.glob("leads_*.csv")):
        if p.name == "leads_world_all.csv":
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
    print("  COMBINED: %d" % len(all_rows))
    for k, v in collections.Counter(r["country"] for r in all_rows).most_common():
        print("    %s: %d" % (k, v))
    print("=" * 56)


if __name__ == "__main__":
    main()
