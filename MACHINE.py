"""
MACHINE.py — harvest -> probe -> send, forever, until 1000 CONFIRMED sends.

No babysitting. Every stage is resumable and db-scoped so nothing double-sends.

TARGET: count of status='submitted' from THIS campaign's sends.

DESIGN NOTES / hard-won
  * Apify is dead (no credit) -> OpenStreetMap Overpass, free.
  * Only maps.mail.ru serves world data; it 504s on big unions, so every country
    is split into small bbox chunks that DO return. overpass-api.de flaps.
  * OSM tags a website on ~30% of records, so breadth comes from many shop AND
    craft values AND local-language name patterns per country.
  * Sends go through run_batch.py (pending-only => never double-sends) with the
    browser tier + NoneCap solver.
  * Probe before send so we only hit pages that actually have a form.

State lives on disk after EVERY unit, so a kill loses nothing.
"""
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = str(BASE / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable

WORLD = BASE / "leads_world"
WORLD.mkdir(exist_ok=True)
LOGF = WORLD / "_machine.log"
DONE = WORLD / "_machine_done.json"

CONFIRM_TARGET = 1000

# ── Overpass ─────────────────────────────────────────────────────────────────
MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
SHOP = ("jewelry|jewellery|watches|antiques|antique|second_hand|pawnbroker|"
        "gold_buyer|collector|vintage|charity|clothes|bag|fashion_accessories")
CRAFT = "jeweller|watchmaker|goldsmith|silversmith|clockmaker"

WEBSITE_TAGS = ["website", "contact:website", "url", "website:en", "contact:url"]
SCHEME = re.compile(r"^(https?://)?(www\.)?", re.I)
BAD = re.compile(r"facebook\.|instagram\.|twitter\.|google\.|yelp\.|tripadvisor\.|"
                 r"foursquare\.|linkedin\.|youtube\.|wikipedia\.|pinterest\.", re.I)
FIELDS = ["country", "domain", "site", "name", "phone", "category",
          "city", "postcode", "street", "source"]

import httpx
UA = {"User-Agent": "EasyListingsLeadHarvest/1.0 (info@easylistings.co.uk)"}


def grid(s, w, n, e, rows, cols):
    out = []
    dh, dw = (n - s) / rows, (e - w) / cols
    for i in range(rows):
        for j in range(cols):
            out.append((round(s + i * dh, 4), round(w + j * dw, 4),
                        round(s + (i + 1) * dh, 4), round(w + (j + 1) * dw, 4)))
    return out


# name regex per country (local language) -- catches mistagged businesses
NAME_RE = {
    "fr": r"bijou|joaill|horlog|antiqu|brocante|orfev|montre|argenterie|diamant|occasion|seconde",
    "it": r"gioiell|orolog|antiqu|orefic|argent|diamant|usato|compro oro",
    "es": r"joyer|reloj|antig|plata|segundamano|ocasi|diamant|almoneda|empeno",
    "de": r"schmuck|juwel|uhren|antik|goldschmied|silber|gebraucht|antiquit",
    "nl": r"juwel|sieraad|horloge|antiek|goud|zilver|tweedehands",
    "be": r"bijou|juwel|horloge|antiek|goud|zilver",
    "at": r"schmuck|juwel|uhren|antik|goldschmied",
    "ch": r"bijou|schmuck|uhren|antik|orfevre|juwel|montre",
    "pt": r"joalh|relog|antig|ouriv|prata|diamant|usad",
    "se": r"smycke|klock|antik|guld|silver",
    "dk": r"smykke|ur|antik|guld|solver",
    "no": r"smykke|klokke|antikk|gull|solv",
    "fi": r"koru|kello|antiikki|kulta|hopea",
    "ie": r"jewel|antique|vintage|watch|gold|silver|pawn",
    "gb": r"jewel|antique|vintage|watch|gold|silver|pawn|estate",
    "au": r"jewel|antique|vintage|watch|gold|silver|pawn",
    "nz": r"jewel|antique|vintage|watch|gold|silver",
    "ae": r"jewel|gold|watch|antique|silver|diamond",
    "ca": r"jewel|antique|vintage|pawn|watch|gold|silver",
    "us": r"jewel|antique|vintage|pawn|watch|gold|silver|estate|consign",
}

# bbox per country. US and the big ones are split harder because they 504.
BBOX = {
    "fr": grid(42.3, -5.2, 51.1, 8.3, 4, 4),
    "it": grid(36.6, 6.6, 47.1, 18.5, 3, 3),
    "es": grid(36.0, -9.4, 43.8, 4.4, 3, 3),
    "de": grid(47.2, 5.8, 55.1, 15.1, 3, 3),
    "nl": grid(50.7, 3.3, 53.6, 7.3, 2, 2),
    "be": grid(49.4, 2.5, 51.6, 6.5, 2, 2),
    "at": grid(46.3, 9.5, 49.1, 17.2, 2, 2),
    "ch": grid(45.8, 5.9, 47.9, 10.5, 2, 2),
    "pt": grid(36.9, -9.6, 42.2, -6.1, 2, 2),
    "se": grid(55.3, 10.9, 69.1, 24.2, 2, 2),
    "dk": grid(54.5, 8.0, 57.8, 15.2, 1, 2),
    "no": grid(57.9, 4.6, 71.2, 31.1, 2, 2),
    "fi": grid(59.7, 19.5, 70.1, 31.6, 2, 1),
    "ie": grid(51.3, -10.7, 55.5, -5.8, 2, 2),
    "gb": grid(49.8, -8.7, 59.0, 1.9, 4, 4),
    "au": grid(-43.7, 112.9, -10.6, 153.6, 3, 3),
    "nz": grid(-47.3, 166.3, -34.3, 178.6, 2, 1),
    "ae": grid(22.6, 51.5, 26.1, 56.4, 1, 2),
    "ca": grid(42.0, -141.0, 70.0, -52.6, 3, 4),
    "us": grid(24.5, -125.0, 49.4, -66.9, 4, 5),
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_done():
    return set(json.loads(DONE.read_text())) if DONE.exists() else set()


def save_done(s):
    DONE.write_text(json.dumps(sorted(s)))


def fetch_box(box, tries=4, timeout=110):
    s, w, n, e = box
    q = (f'[out:json][timeout:100];'
         f'nwr["shop"~"^({SHOP})$"]({s},{w},{n},{e});'
         f'nwr["craft"~"^({CRAFT})$"]({s},{w},{n},{e});out tags center;')
    for a in range(tries):
        for m in MIRRORS:
            try:
                r = httpx.post(m, data={"data": q}, headers=UA, timeout=timeout)
                if r.status_code == 200:
                    els = r.json().get("elements", [])
                    if els:
                        return els
            except Exception:
                pass
        time.sleep(3 + a * 5)
    return []


def rows_from(els, cc):
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
        out.append({"country": cc.upper(), "domain": dom, "site": site,
                    "name": (t.get("name") or "").strip(),
                    "phone": (t.get("phone") or t.get("contact:phone") or "").strip(),
                    "category": (t.get("shop") or t.get("craft") or "").strip(),
                    "city": t.get("addr:city", ""),
                    "postcode": t.get("addr:postcode", ""),
                    "street": " ".join(x for x in [t.get("addr:housenumber", ""),
                                                   t.get("addr:street", "")] if x),
                    "source": "osm_%s/%s" % (el.get("type"), el.get("id"))})
    return out


def save_country(cc, new):
    p = WORLD / ("leads_%s.csv" % cc)
    have = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            have = list(csv.DictReader(f))
    seen = {r["domain"] for r in have}
    a = 0
    for r in new:
        if r["domain"] not in seen:
            seen.add(r["domain"])
            have.append(r)
            a += 1
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(have)
    return a, len(have)


def harvest():
    done = load_done()
    order = ["fr", "it", "es", "de", "gb", "us", "nl", "be", "at", "se", "dk",
             "no", "fi", "pt", "au", "nz", "ae", "ie", "ch", "ca"]
    for cc in order:
        boxes = BBOX.get(cc)
        if not boxes:
            continue
        for i, box in enumerate(boxes, 1):
            unit = "%s:%d" % (cc, i)
            if unit in done:
                continue
            els = fetch_box(box)
            if els:
                got = rows_from(els, cc)
                if got:
                    a, tot = save_country(cc, got)
                    if a:
                        log("  %s [%d/%d] +%d -> %d" % (cc.upper(), i, len(boxes), a, tot))
            done.add(unit)
            save_done(done)


def rebuild_all_txt():
    seen, sites = set(), []
    for p in sorted(WORLD.glob("leads_*.csv")):
        if "world_all" in p.name or "probe" in p.name:
            continue
        for r in csv.DictReader(open(p, encoding="utf-8")):
            if r["domain"] in seen:
                continue
            seen.add(r["domain"])
            if r.get("site"):
                sites.append(r["site"])
    txt = WORLD / "leads_world_all.txt"
    txt.write_text("\n".join(sorted(sites)) + "\n", encoding="utf-8")
    return len(sites)


def confirmed_count():
    import sqlite3
    c = sqlite3.connect(str(BASE / "data" / "formblaster.db"))
    try:
        v = c.execute("select count(*) from sites where status='submitted' "
                      "and tier='browser'").fetchone()[0]
    finally:
        c.close()
    return v


def run(args, timeout=7200, tail=4):
    try:
        p = subprocess.run(args, cwd=str(BASE), timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        for l in (p.stdout or "").strip().splitlines()[-tail:]:
            log("    " + l)
        return p.returncode
    except subprocess.TimeoutExpired:
        log("    TIMEOUT")
        return -1


def main():
    log("=" * 64)
    log("MACHINE START — target %d confirmed browser submits" % CONFIRM_TARGET)
    log("=" * 64)
    rnd = 0
    while True:
        rnd += 1
        got = confirmed_count()
        log("")
        log("### ROUND %d — confirmed so far: %d / %d" % (rnd, got, CONFIRM_TARGET))

        log("-- harvest --")
        harvest()
        n = rebuild_all_txt()
        log("   leads in pool: %d" % n)

        log("-- probe --")
        run([PY, "probe_forms.py", str(WORLD / "leads_world_all.txt"),
             "--concurrency", "24"], timeout=10800, tail=3)

        log("-- send --")
        forms = WORLD / "leads_world_all_forms.txt"
        if forms.exists():
            run([PY, "run_batch.py", "--input", str(forms),
                 "--concurrency", "8", "--site-budget", "70"],
                timeout=21600, tail=8)

        got = confirmed_count()
        log("### confirmed now: %d" % got)
        if got >= CONFIRM_TARGET:
            log("*** TARGET HIT: %d confirmed ***" % got)
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
