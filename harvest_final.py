"""
harvest_final.py — lead harvest that survives Overpass being flaky.

WHAT WAS LEARNED THE HARD WAY (2026-11-04, 02:00-03:00 UTC)

  * overpass-api.de is the main mirror and the ONLY one with full world data,
    but it flaps: ConnectTimeout for minutes at a time. Retrying it in a tight
    loop burns the whole budget. It needs LONG waits between attempts.
  * overpass.kumi.systems and overpass.openstreetmap.ru are down entirely.
  * overpass.osm.ch answers in <1s but is SWITZERLAND-ONLY. Returns 0 for NL/BE.
    Perfect for CH, useless as a general mirror.
  * overpass.openstreetmap.fr 403s.
  * A 7-country union in one shot gets throttled. One query at a time.
  * "http200 but 0 elements" on a country query = throttled, or wrong mirror.
  * The nationwide US query times out; per-STATE ([ISO3166-2="US-TX"]) works.

DESIGN
  1. Try mirrors in priority order, with health ranking that demotes whatever
     just failed and promotes whatever just worked.
  2. Persistent per-unit output: every unit is saved the moment it lands, so a
     kill/crash never loses progress. Re-running skips completed units.
  3. Long, escalating backoff. This is an overnight job, not a benchmark.
  4. Units are (country) for small countries, (US state) for the US.

    python harvest_final.py --countries ie fr it es pt de nl be at se dk au nz ae ca ch
    python harvest_final.py --us
    python harvest_final.py --status
"""

import argparse
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
STATE = OUT / "_harvest_state.json"
DONE = OUT / "_done_units.json"

UA = {"User-Agent": "EasyListingsLeadHarvest/1.0 (info@easylistings.co.uk)"}

# Priority mirrors. MEASURED 2026-11-04 03:20 UTC:
#   overpass-api.de        ConnectTimeout 42s+  (down, was primary)
#   overpass.kumi.systems  ReadTimeout         (down)
#   overpass.private.coffee ReadTimeout        (down)
#   maps.mail.ru           200 in 12s, n=361   <-- WORKS, full world data
#   overpass.osm.ch        200 in 0.5s but CH-ONLY
# Put the working mirror FIRST so we stop burning budget on dead ones.
MIRRORS_WORLD = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# osm.ch is CH-only. Never use it for other countries or it returns empty 200s.
MIRROR_CH = ["https://overpass.osm.ch/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]

COUNTRIES = {
    "ie": ("IE", r"jewel|antique|vintage|watch|gold|silver|pawn"),
    "fr": ("FR", r"bijou|joaill|horlog|antiqu|brocante|orfev|montre|argenterie|diamant"),
    "it": ("IT", r"gioiell|orolog|antiqu|orefic|argent|diamant"),
    "es": ("ES", r"joyer|reloj|antig|plata|segunda mano|ocasi|diamant"),
    "pt": ("PT", r"joalh|relog|antig|ouriv|prata|diamant"),
    "de": ("DE", r"schmuck|juwel|uhren|antik|goldschmied|silber|gebraucht"),
    "nl": ("NL", r"juwel|sieraad|horloge|antiek|goud|zilver|tweedehands"),
    "be": ("BE", r"bijou|juwel|horloge|antiek|goud|zilver"),
    "ch": ("CH", r"bijou|schmuck|uhren|antik|orfevre|juwel"),
    "at": ("AT", r"schmuck|juwel|uhren|antik|goldschmied"),
    "se": ("SE", r"smycke|klock|antik|guld|silver"),
    "dk": ("DK", r"smykke|ur|antik|guld|solver"),
    "no": ("NO", r"smykke|klokke|antikk|gull|solv"),
    "fi": ("FI", r"koru|kello|antiikki|kulta|hopea"),
    "au": ("AU", r"jewel|antique|vintage|watch|gold|silver|pawn"),
    "nz": ("NZ", r"jewel|antique|vintage|watch|gold|silver"),
    "ae": ("AE", r"jewel|gold|watch|antique|silver|diamond"),
    "ca": ("CA", r"jewel|antique|vintage|pawn|watch|gold|silver"),
}

US_STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL",
             "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT",
             "NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI",
             "SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"]

SHOP_RE = ("jewelry|jewellery|watches|antiques|antique|second_hand|"
           "pawnbroker|gold_buyer|collector|vintage")
CRAFT_RE = "jeweller|watchmaker|goldsmith|silversmith|clockmaker"
WEBSITE_TAGS = ["website", "contact:website", "url", "website:en", "contact:url"]

SCHEME = re.compile(r"^(https?://)?(www\.)?", re.I)
BAD = re.compile(r"facebook\.|instagram\.|twitter\.|google\.|yelp\.|tripadvisor\.|"
                 r"foursquare\.|linkedin\.|youtube\.|wikipedia\.|pinterest\.", re.I)
FIELDS = ["country", "domain", "site", "name", "phone", "category",
          "city", "postcode", "street", "source"]


# ── state ────────────────────────────────────────────────────────────────────
def load_done():
    if DONE.exists():
        return set(json.loads(DONE.read_text()))
    return set()


def mark_done(unit):
    d = load_done()
    d.add(unit)
    DONE.write_text(json.dumps(sorted(d)))


# ── mirror health ────────────────────────────────────────────────────────────
_health = {}


def mirrors_for(cc):
    pool = MIRROR_CH if cc == "ch" else MIRRORS_WORLD
    return sorted(pool, key=lambda m: -_health.get(m, 0))


def note(m, ok):
    _health[m] = _health.get(m, 0) + (2 if ok else -1)


# ── query ────────────────────────────────────────────────────────────────────
def build_q(selector, name_re=None):
    body = (f'  nwr["shop"~"^({SHOP_RE})$"](area.a);\n'
            f'  nwr["craft"~"^({CRAFT_RE})$"](area.a);\n')
    if name_re:
        body += (f'  nwr["name"~"{name_re}",i]["shop"](area.a);\n'
                 f'  nwr["name"~"{name_re}",i]["craft"](area.a);\n')
    return ('[out:json][timeout:240][maxsize:1073741824];\n'
            f'{selector}->.a;\n(\n{body});\nout tags center;')


def fetch(q, cc, tries=6):
    for a in range(tries):
        for m in mirrors_for(cc):
            try:
                r = httpx.post(m, data={"data": q}, headers=UA, timeout=280)
                if r.status_code == 200:
                    els = r.json().get("elements", [])
                    if els:
                        note(m, True)
                        return els
                    note(m, False)
                else:
                    note(m, False)
            except Exception:
                note(m, False)
        # 8s base: we now have a live mirror, so long sleeps waste throughput
        wait = 8 + a * 10
        print("      all mirrors failed, sleep %ds" % wait, flush=True)
        time.sleep(wait)
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
    print("   %s: +%d -> %d total" % (p.name, added, len(have)), flush=True)
    return len(have)


# ── units ────────────────────────────────────────────────────────────────────
def do_country(iso):
    unit = "c:" + iso
    if unit in load_done():
        print("=== %s DONE, skip ===" % iso.upper(), flush=True)
        return
    area, name_re = COUNTRIES[iso]
    print("\n=== %s ===" % iso.upper(), flush=True)
    total = 0
    for i, name in enumerate([None, name_re], 1):
        print("   pass %d/2" % i, flush=True)
        els = fetch(build_q(f'area["ISO3166-1"="{area}"][admin_level=2]', name), iso)
        got = rows(els, iso.upper())
        if got:
            total = save(iso, got)
        print("      got %d" % len(got), flush=True)
        time.sleep(5)
    if total:
        mark_done(unit)
    else:
        print("   !! %s yielded nothing" % iso.upper(), flush=True)


def do_us_state(st):
    unit = "s:US-" + st
    if unit in load_done():
        return
    print("  US-%s" % st, flush=True)
    els = fetch(build_q(f'area["ISO3166-2"="US-{st}"][admin_level=4]'), "us")
    got = rows(els, "US")
    if got:
        save("us", got)
        mark_done(unit)
    time.sleep(3)


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
    print("\n" + "=" * 58)
    print("  COMBINED TOTAL: %d" % len(all_rows))
    for k, v in collections.Counter(r["country"] for r in all_rows).most_common():
        print("    %-4s %d" % (k, v))
    print("=" * 58)
    return len(all_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="*", default=None)
    ap.add_argument("--us", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset and DONE.exists():
        DONE.unlink()
    if args.status:
        d = load_done()
        print("units done: %d" % len(d))
        for u in sorted(d):
            print("  ", u)
        combine()
        return

    if args.us:
        for st in US_STATES:
            do_us_state(st)
        combine()
        return

    for iso in (args.countries or list(COUNTRIES)):
        if iso in COUNTRIES:
            do_country(iso)
    combine()


if __name__ == "__main__":
    main()
