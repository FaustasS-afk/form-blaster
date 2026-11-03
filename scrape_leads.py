"""
scrape_leads.py — harvest US antique/vintage/jewellery/watch business websites.

Walks localvintagestores.com:  state -> city -> store -> outbound website.
Each store page carries schema.org Store JSON-LD including the business's
real website and phone, which is exactly what Form Blaster needs.

  python scrape_leads.py --states texas,florida --max-cities 5
  python scrape_leads.py --all
  python scrape_leads.py --status
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core import db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HOST = "localvintagestores.com"

# Domains that are never the business's own site
JUNK = re.compile(
    r"(facebook|instagram|twitter|linkedin|youtube|tiktok|pinterest|"
    r"google\.|yelp|apple\.com|maps\.|imagedelivery|84em|wordpress\.org|"
    r"schema\.org|w3\.org|squarespace\.com$|godaddy|wixsite\.com$)",
    re.I,
)

# National chains that appear in every city. Real businesses, but their contact
# form is corporate-level, so they add noise to a local campaign.
CHAINS = re.compile(
    r"(goodwill|habitatncc|valuevillage|platoscloset|onceuponachild|"
    r"salvationarmy|satruck|2ndswing|primethrift|thesecondchanceshoppe|"
    r"savers|buffaloexchange|crossroadstrading|uptowncheapskate|clothesmentor)",
    re.I,
)

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


def client():
    return httpx.Client(
        follow_redirects=True,
        timeout=25,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    )


def get(c, url, retries=2):
    for attempt in range(retries + 1):
        try:
            r = c.get(url)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                time.sleep(1.5 + attempt)
                continue
        except Exception:
            time.sleep(1.0)
    return None


def ld_blocks(html):
    s = BeautifulSoup(html, "lxml")
    out = []
    for sc in s.find_all("script", type="application/ld+json"):
        try:
            out.append(json.loads(sc.string))
        except Exception:
            pass
    return out, s


def city_urls(html):
    """ItemList on a state page -> list of city page URLs."""
    urls = []
    for d in ld_blocks(html)[0]:
        if d.get("@type") != "ItemList":
            continue
        for it in d.get("itemListElement", []):
            item = it.get("item", {})
            u = item.get("url")
            if u and "/store/" not in u:
                urls.append(u)
    return urls


def store_urls(html):
    """ItemList on a city page -> list of store detail page URLs."""
    urls = []
    for d in ld_blocks(html)[0]:
        if d.get("@type") != "ItemList":
            continue
        for it in d.get("itemListElement", []):
            item = it.get("item", {})
            u = item.get("url")
            if u and "/store/" in u:
                urls.append(u)
    return urls


def store_website(html):
    """
    From a store page's JSON-LD, return (website, phone, name, category).
    The Store schema carries the business's real outbound URL.
    """
    website = phone = name = None
    cat = None
    for d in ld_blocks(html)[0]:
        t = d.get("@type")
        if not t or t == "BreadcrumbList":
            continue
        if isinstance(t, list):
            t = t[0] if t else ""
        if "Store" in str(t) or "Shop" in str(t) or "Place" in str(t) or "Business" in str(t):
            website = d.get("url") or d.get("sameAs")
            phone = d.get("telephone")
            name = d.get("name")
            cat = t
            if website:
                break
    return website, phone, name, cat


def clean_site(u, drop_chains=False):
    """Normalise a business URL: strip tracking params, drop junk hosts."""
    if not u or not u.startswith("http"):
        return None
    p = urlparse(u)
    host = p.netloc.lower()
    if not host or JUNK.search(u):
        return None
    if HOST in host:
        return None
    if drop_chains and CHAINS.search(host + p.path):
        return None
    # Drop query strings that are just tracking / picker noise
    if p.query and re.search(r"(utm_|gmb|fbc|gclid)", p.query, re.I):
        return f"{p.scheme}://{p.netloc}{p.path}"
    return u


def norm_domain(u):
    """Canonical host for dedupe: lowercase, no www., no port."""
    try:
        d = urlparse(u).netloc.lower().split(":")[0]
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


# ── crawl ────────────────────────────────────────────────────────────────────

def crawl(states, max_cities=None, workers=6, delay=0.4):
    c = client()
    found = {}
    cities_done = 0

    for st in states:
        st_url = f"https://{HOST}/{st}/"
        print(f"\n=== {st} ===", flush=True)
        html = get(c, st_url)
        if not html:
            print("  state page failed")
            continue

        cities = city_urls(html)
        if max_cities:
            cities = cities[:max_cities]
        print(f"  {len(cities)} city page(s)", flush=True)

        for cu in cities:
            ch = get(c, cu)
            if not ch:
                continue
            stores = store_urls(ch)
            cities_done += 1
            hits = 0
            for su in stores:
                sh = get(c, su)
                if not sh:
                    continue
                site, phone, name, cat = store_website(sh)
                site = clean_site(site)
                if site:
                    dom = norm_domain(site)
                    if dom and dom not in found:
                        found[dom] = {
                            "site": site, "phone": phone, "name": name,
                            "category": cat, "source": su,
                        }
                        hits += 1
                time.sleep(delay * 0.4)
            print(f"    {cu.split('/')[-2]:<22} {len(stores):>3} stores, "
                  f"{hits:>3} new sites  (total {len(found)})", flush=True)

    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default="texas")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-cities", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="leads.txt")
    ap.add_argument("--sites-out", default="leads_sites.csv")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--drop-chains", action="store_true",
                    help="exclude national chains (Goodwill, Habitat, Plato's Closet...)")
    args = ap.parse_args()

    if args.status:
        p = BASE / "leads_sites.csv"
        if p.exists():
            n = sum(1 for _ in open(p, encoding="utf-8")) - 1
            print(f"{n} leads in {p}")
        else:
            print("no leads file yet")
        return

    states = STATES if args.all else [s.strip() for s in args.states.split(",") if s.strip()]
    print(f"Scraping {len(states)} state(s) from {HOST}")
    t0 = time.time()
    found = crawl(states, args.max_cities or None, args.workers)
    print(f"\ndone in {(time.time()-t0)/60:.1f} min — {len(found)} unique business websites")

    # write leads.txt (homepages) — Form Blaster crawls /contact itself
    with open(BASE / args.out, "w", encoding="utf-8") as f:
        for d in sorted(found):
            f.write(found[d]["site"] + "\n")

    # write csv with metadata for reference
    import csv
    with open(BASE / args.sites_out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "site", "name", "phone", "category", "source"])
        for d in sorted(found):
            r = found[d]
            w.writerow([d, r["site"], r["name"], r["phone"], r["category"], r["source"]])

    print(f"wrote {args.out} and {args.sites_out}")


if __name__ == "__main__":
    main()
