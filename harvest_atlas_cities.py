"""
harvest_atlas_cities.py — city-level sweep of antiquestoreatlas.com.

Why: harvest_atlas_themes.py walks theme/STATE pages and got everything the
state pages link. The atlas ALSO has a page per theme/state/CITY
(/jewelry/california/los-angeles/), and those carry dealers the state page does
not — a state page lists ~150 for a big state, but its cities cumulatively list
more, because the state page is a subset.

The city URLs come straight from the site's own sitemaps, so there is no slug
guessing: sitemap-filter-<theme>.xml enumerates every theme/state/city page.

  python harvest_atlas_cities.py                  # all themes from sitemaps
  python harvest_atlas_cities.py --themes jewelry
  python harvest_atlas_cities.py --max-pages 20

Output: leads_atlas_cities.csv (+ .txt), same 6-column shape as the others.
Dedupes against every existing leads_*.csv and itself. Atomic write per page,
and a .done file so --resume can continue after an interruption.
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
HOST = "antiquestoreatlas.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

THEMES = ["jewelry", "antique-malls", "consignment", "estate-sales",
          "appraisal-services", "furniture", "open-sunday"]

SKIP = re.compile(
    r"facebook|twitter|instagram|youtube|pinterest|linkedin|tiktok|"
    r"google|cloudflare|wp\.org|schema|w3\.org|apple\.com|adobe|amazon|"
    r"ebay|paypal|gstatic|doubleclick|onetrust|cookiebot|hub\.biz|"
    r"overturemaps|openstreetmap|openfreemap|news\.google|creativecommons|"
    r"trustpilot|yelp|yellowpages|mapquest|bing\.com|yahoo\.com|"
    r"squarespace\.com|wixsite\.com|weebly\.com|wordpress\.com|"
    r"godaddy|network-solutions|constantcontact|mailchimp|formstack|"
    r"jotform|wufoo|typeform|calendly|docusign",
    re.I,
)
SKIP_SUB = re.compile(r"^(www\.)?(shop|store|my|blog|go|link|links)\.", re.I)

OUT_CSV = BASE / "leads_atlas_cities.csv"
OUT_TXT = BASE / "leads_atlas_cities.txt"
DONE = BASE / "leads_atlas_cities.done"


def client():
    return httpx.Client(follow_redirects=True, timeout=30,
                        headers={"User-Agent": UA,
                                 "Accept-Language": "en-US,en;q=0.9"})


def get(c, url, tries=3):
    for n in range(tries):
        try:
            r = c.get(url)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                time.sleep(1.5 * (n + 1))
                continue
            return None
        except Exception:
            time.sleep(1.2 * (n + 1))
    return None


def city_urls_from_sitemaps(c, themes):
    """Every theme/state/city page, straight from the site's own sitemap."""
    urls = []
    for t in themes:
        xml = get(c, f"https://{HOST}/sitemap-filter-{t}.xml")
        if not xml:
            print(f"  no sitemap for theme {t}")
            continue
        locs = re.findall(r"<loc>(.*?)</loc>", xml)
        # city pages have 3 path segments after the host: /theme/state/city/
        cities = [u for u in locs if len([p for p in urlparse(u).path.split("/") if p]) == 3]
        print(f"  {t:<20} {len(locs):>5} urls, {len(cities):>5} city pages")
        urls += cities
        time.sleep(0.3)
    return urls


def domains_on_page(html):
    s = BeautifulSoup(html, "lxml")
    out = set()
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if not h.startswith("http"):
            continue
        host = urlparse(h).netloc.lower()
        if not host or HOST in host or SKIP.search(host) or SKIP_SUB.match(host):
            continue
        out.add(host)
    return out


def norm(h):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--themes", default=",".join(THEMES))
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    seen = load_known()
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if (args.resume and DONE.exists()) else set()

    rows = {}
    if OUT_CSV.exists():
        with open(OUT_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("domain"):
                    rows[row["domain"]] = row

    print(f"known domains: {len(seen)}  already-done pages: {len(done)}")
    c = client()
    print("\nenumerating city pages from sitemaps:")
    urls = city_urls_from_sitemaps(c, themes)
    if args.max_pages:
        urls = urls[:args.max_pages]
    print(f"\n{len(urls)} city pages to crawl\n")

    added = 0
    for i, u in enumerate(urls, 1):
        if u in done:
            continue
        html = get(c, u)
        if not html:
            with open(DONE, "a", encoding="utf-8") as f:
                f.write(u + "\n")
            continue
        new = 0
        for host in domains_on_page(html):
            d = norm(host)
            if d in seen or d in rows:
                continue
            seen.add(d)
            rows[d] = {"domain": d, "site": f"https://{host}/", "name": "",
                       "phone": "", "category": u.split("/")[3] if len(u.split("/")) > 3 else "",
                       "source": u}
            new += 1
        added += new
        with open(DONE, "a", encoding="utf-8") as f:
            f.write(u + "\n")
        if new:
            save(rows)
        if i % 10 == 0 or new:
            print(f"  [{i}/{len(urls)}] +{new:<4} new  (total {len(rows)})  "
                  f"{urlparse(u).path}", flush=True)
        time.sleep(args.delay)

    save(rows)
    print(f"\ndone — +{added} new, {len(rows)} total")
    print(f"  {OUT_CSV.name}\n  {OUT_TXT.name}")


if __name__ == "__main__":
    main()
