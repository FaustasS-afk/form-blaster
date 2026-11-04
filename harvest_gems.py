"""
harvest_gems.py â€” US jewellery/furniture dealer leads from plain-HTTP sources.

BrightData is returning 502 on every request (verified 2026-11-03), so any
source that needs the unlocker is out. These sources answer to a plain fetch:

  * antiquestoreatlas.com  â€” theme x state pages (5 themes, works, ~1k/theme)
  * localvintagestores.com â€” the original source, re-crawlable for NEW states
  * hotfrog.com            â€” 200, ~48 external per page
  * merchantcircle.com     â€” 200

Design: every source yields "external host links on a listing page". We keep
the host, drop aggregators/CDNs/socials, dedupe against every existing
leads_*.csv, and write one combined CSV that merge.py can consume.

  python harvest_gems.py --source atlas       # theme sweep, all states
  python harvest_gems.py --source hotfrog     # category x state
  python harvest_gems.py --source lvs
  python harvest_gems.py --all

Output: leads_gems.csv / .txt, 6-column shape matching the others.
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, quote

import httpx
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
OUT_CSV = BASE / "leads_gems.csv"
OUT_TXT = BASE / "leads_gems.txt"
DONE = BASE / "leads_gems.done"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"}

STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new-hampshire",
    "new-jersey", "new-mexico", "new-york", "north-carolina", "north-dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode-island",
    "south-carolina", "south-dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west-virginia", "wisconsin", "wyoming",
]

# Aggregators / infra / socials â€” never the dealer's own site.
SKIP = re.compile(
    r"facebook|twitter|instagram|youtube|pinterest|linkedin|tiktok|snapchat|"
    r"google|cloudflare|wp\.org|schema|w3\.org|apple\.com|adobe\.com|amazon|"
    r"ebay|paypal|gstatic|doubleclick|onetrust|cookiebot|hub\.biz|"
    r"overturemaps|openstreetmap|openfreemap|news\.google|creativecommons|"
    r"trustpilot|yelp|mapquest|bing\.com|yahoo\.com|tripadvisor|"
    r"squarespace\.com|wixsite\.com|weebly\.com|wordpress\.com|shopify\.com|"
    r"godaddy|network-solutions|constantcontact|mailchimp|formstack|jotform|"
    r"wufoo|typeform|calendly|docusign|hotfrog|merchantcircle|cylex|"
    r"superpages|yellowpages|manta|chamberofcommerce|bizapedia|bbb\.org|"
    r"mapbox|arcgis|gstatic|jsdelivr|unpkg|bootstrapcdn|fontawesome|"
    r"gofundme|stripe|squareup|intuit|wix\.com|duda\.co|webflow\.io|"
    r"\.dk$|\.co\.uk$|\.au$|\.nz$|hotfrog\.|blog\.hotfrog",
    re.I,
)
SKIP_SUB = re.compile(r"^(www\.)?(shop|store|my|blog|go|link|links|static|api|cdn)\.", re.I)


def client():
    return httpx.Client(follow_redirects=True, timeout=25, headers=H)


def get(c, url, tries=2):
    for n in range(tries):
        try:
            r = c.get(url)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                time.sleep(1.0 * (n + 1))
                continue
            return None
        except Exception:
            time.sleep(0.6)
    return None


def hosts_on(html, self_host):
    s = BeautifulSoup(html, "lxml")
    out = set()
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if not h.startswith("http"):
            continue
        host = urlparse(h).netloc.lower()
        if not host or self_host in host:
            continue
        if SKIP.search(host) or SKIP_SUB.match(host):
            continue
        out.add(host)
    return out


def norm(h):
    h = h.split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def load_known():
    seen = set()
    for p in BASE.glob("leads_*.csv"):
        try:
            with open(p, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = (row.get("domain") or "").strip().lower()
                    if d:
                        seen.add(norm(d))
        except Exception:
            pass
    return seen


def save(rows):
    tmp = OUT_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "site", "name", "phone",
                                          "category", "source"])
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    tmp.replace(OUT_CSV)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        for d in sorted(rows):
            f.write((rows[d].get("site") or f"https://{d}") + "\n")


# â”€â”€ source definitions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def pages_atlas():
    themes = ["furniture", "jewelry", "antique-malls", "consignment",
              "estate-sales", "appraisal-services", "open-sunday"]
    for t in themes:
        for st in STATES:
            yield f"https://antiquestoreatlas.com/{t}/{st}/", t


def pages_hotfrog():
    """
    Hotfrog wants the TWO-letter state code (/search/CA/jewelry). A single
    letter (/search/A/jewelry) returns a thin page carrying only social links,
    so a naive first-letter bucket finds nothing.
    """
    cats = ["jewelry", "antique", "furniture", "consignment", "estate"]
    codes = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT",
        "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
        "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
        "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
        "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
        "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
        "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new-hampshire": "NH", "new-jersey": "NJ", "new-mexico": "NM",
        "new-york": "NY", "north-carolina": "NC", "north-dakota": "ND",
        "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
        "rhode-island": "RI", "south-carolina": "SC", "south-dakota": "SD",
        "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
        "virginia": "VA", "washington": "WA", "west-virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY",
    }
    for cat in cats:
        for code in codes.values():
            yield (f"https://www.hotfrog.com/search/{code}/{cat}", cat)


def pages_lvs():
    for st in STATES:
        yield f"https://localvintagestores.com/{st}/", "vintage"
        yield f"https://localvintagestores.com/{st}/jewelry/", "jewelry"


SOURCES = {"atlas": pages_atlas, "hotfrog": pages_hotfrog, "lvs": pages_lvs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="atlas", choices=list(SOURCES))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.35)
    args = ap.parse_args()

    names = list(SOURCES) if args.all else [args.source]
    seen = load_known()
    rows = {}
    if OUT_CSV.exists():
        with open(OUT_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("domain"):
                    rows[row["domain"]] = row

    print(f"known domains: {len(seen)}  existing gem rows: {len(rows)}")

    c = client()
    added = 0
    for name in names:
        gen = list(SOURCES[name]())
        if args.max:
            gen = gen[:args.max]
        print(f"\n-- source {name}: {len(gen)} pages --", flush=True)
        for i, (url, cat) in enumerate(gen, 1):
            html = get(c, url)
            if not html:
                if i % 10 == 0:
                    print(f"   [{i}/{len(gen)}] -", flush=True)
                continue
            selfh = urlparse(url).netloc.lower()
            new = 0
            for host in hosts_on(html, selfh):
                d = norm(host)
                if d in seen or d in rows:
                    continue
                seen.add(d)
                rows[d] = {"domain": d, "site": f"https://{host}/", "name": "",
                           "phone": "", "category": cat, "source": url}
                new += 1
            added += new
            if new:
                save(rows)
            if i % 10 == 0 or new:
                print(f"   [{i}/{len(gen)}] +{new:<4} (total {len(rows)})", flush=True)
            time.sleep(args.delay)

    save(rows)
    print(f"\ndone â€” +{added} new, {len(rows)} total in {OUT_CSV.name}")


if __name__ == "__main__":
    main()

