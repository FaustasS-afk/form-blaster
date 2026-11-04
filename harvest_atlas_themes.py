"""
harvest_atlas_themes.py — NEW US dealer leads from antiquestoreatlas.com.

Why this exists: leads_master.csv already holds ~10,000 leads from
localvintagestores.com and one atlas 'jewelry' pass. To get ANOTHER 10,000
jewellery-adjacent US dealers, this walks every VALID theme x state page on the
atlas, which is a different slice of the same directory plus four whole themes
the first pass never touched.

Measured 2026-11-03: 5 valid themes x 50 states = 250 pages. Per-page yield
averages ~69 outbound dealer domains (jewelry pages run 150+ for big states),
so ~17k gross and roughly 9-10k after dedupe against existing leads.

Valid theme slugs were brute-probed; the atlas nav is JS-rendered so scraping
the homepage for them does not work. Do not trust an assumed slug list.

  python harvest_atlas_themes.py                # all themes, all states
  python harvest_atlas_themes.py --themes jewelry,antique-malls
  python harvest_atlas_themes.py --max-states 5 # smoke test
  python harvest_atlas_themes.py --resume       # skip pages already done

Output: leads_atlas_themes.csv (+ .txt), same 6-column shape as the other
harvesters, so merge.py can combine it. Writes atomically after each page.
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
sys.path.insert(0, str(BASE))

HOST = "antiquestoreatlas.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Verified live — every other guessed slug 404s.
THEMES = ["jewelry", "antique-malls", "consignment", "estate-sales", "appraisal-services"]

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

# Aggregators, CDNs and socials — not dealer websites.
SKIP = re.compile(
    r"facebook|twitter|instagram|youtube|pinterest|linkedin|tiktok|"
    r"google|cloudflare|wp\.org|schema|w3\.org|apple\.com|adobe|amazon|"
    r"ebay|paypal|gstatic|doubleclick|onetrust|cookiebot|hub\.biz|"
    r"overturemaps|openstreetmap|openfreemap|news\.google|creativecommons|"
    r"trustpilot|yelp|yellowpages|mapquest|bing\.com|yahoo\.com|"
    r"squarespace\.com|wixsite\.com|weebly\.com|wordpress\.com|"
    r"godaddy|network-solutions|constantcontact|mailchimp",
    re.I,
)

# Ugly self-hosted storefront subdomains that are never the dealer's real site.
SKIP_SUB = re.compile(r"^(www\.)?(shop|store|my|blog|go)\.", re.I)

OUT_CSV = BASE / "leads_atlas_themes.csv"
OUT_TXT = BASE / "leads_atlas_themes.txt"
DONE_FILE = BASE / "leads_atlas_themes.done"


def client():
    return httpx.Client(
        follow_redirects=True, timeout=30,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                 "Accept": "text/html,application/xhtml+xml"},
    )


def get(c, url, tries=3):
    """Retry 403/429 with backoff — the atlas rate-limits bursts."""
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


def domains_on_page(html):
    s = BeautifulSoup(html, "lxml")
    out = set()
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if not h.startswith("http"):
            continue
        u = urlparse(h)
        host = u.netloc.lower()
        if not host or HOST in host or SKIP.search(host) or SKIP_SUB.match(host):
            continue
        if not host.startswith("www."):
            # keep, but normalise so dedupe works against existing www. rows
            pass
        out.add(host)
    return out


def norm(host):
    return host[4:] if host.startswith("www.") else host


def load_existing():
    """Dedupe against everything already harvested, plus this run's output."""
    seen = set()
    for name in ("leads_master.csv", "leads_all.csv", "leads_atlas.csv",
                 "leads_atlas_themes.csv"):
        p = BASE / name
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = (row.get("domain") or "").strip().lower()
                    if d:
                        seen.add(norm(d))
        except Exception:
            pass
    return seen


def load_done():
    if not DONE_FILE.exists():
        return set()
    return {l.strip() for l in DONE_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}


def save(rows, seen):
    tmp = OUT_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "site", "name", "phone",
                                          "category", "source"])
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    tmp.replace(OUT_CSV)  # atomic

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        for d in sorted(rows):
            f.write((rows[d].get("site") or f"https://{d}") + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--themes", default=",".join(THEMES))
    ap.add_argument("--max-states", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="skip pages recorded in the .done file")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    states = STATES[:args.max_states] if args.max_states else STATES

    seen = load_existing()
    done = load_done() if args.resume else set()
    print(f"already-known domains: {len(seen)}")

    rows = {}
    if OUT_CSV.exists():
        with open(OUT_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("domain"):
                    rows[row["domain"]] = row

    total_pages = len(themes) * len(states)
    print(f"themes={len(themes)} states={len(states)} -> {total_pages} pages\n")

    c = client()
    added = 0
    pages = 0
    for theme in themes:
        for st in states:
            key = f"{theme}/{st}"
            if key in done:
                continue
            url = f"https://{HOST}/{theme}/{st}/"
            html = get(c, url)
            pages += 1
            if not html:
                print(f"  {key:<34} -", flush=True)
                with open(DONE_FILE, "a", encoding="utf-8") as f:
                    f.write(key + "\n")
                continue

            new = 0
            for host in domains_on_page(html):
                d = norm(host)
                if d in seen or d in rows:
                    continue
                seen.add(d)
                rows[d] = {"domain": d, "site": f"https://{host}/", "name": "",
                           "phone": "", "category": theme, "source": url}
                new += 1

            added += new
            print(f"  {key:<34} +{new:<4} new  (total {len(rows)})", flush=True)
            with open(DONE_FILE, "a", encoding="utf-8") as f:
                f.write(key + "\n")
            if new:
                save(rows, seen)
            time.sleep(args.delay)

    save(rows, seen)
    print(f"\ndone — {pages} pages, +{added} new, {len(rows)} total")
    print(f"  {OUT_CSV.name}")
    print(f"  {OUT_TXT.name}")


if __name__ == "__main__":
    main()
