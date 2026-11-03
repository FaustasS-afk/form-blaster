"""
harvest_atlas.py — second lead source: antiquestoreatlas.com jewellery slice.

Unlike localvintagestores (which needs state -> city -> store -> website),
this site lists the business's OWN website directly on each state page.
So it is a single fetch per state.

Theme pages available: jewelry, antique-malls, furniture, consignment,
estate-sales, appraisal-services.

  python harvest_atlas.py --theme jewelry
  python harvest_atlas.py --theme jewelry,antique-malls
  python harvest_atlas.py --all

Appends to leads.txt and merges into leads_all.csv, deduping by domain, so it
can be run alongside harvest_all.py.
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from scrape_leads import STATES, clean_site, JUNK

HOST = "antiquestoreatlas.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SKIP_HOSTS = re.compile(
    r"(overturemaps|openstreetmap|openfreemap|news\.google|schema\.org|"
    r"google\.com/maps|wp\.org|gmpg\.org|creativecommons)",
    re.I,
)

THEMES = ["jewelry", "antique-malls", "furniture", "consignment",
          "estate-sales", "appraisal-services"]


def client():
    return httpx.Client(follow_redirects=True, timeout=30,
                        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


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
            time.sleep(1.5 * (n + 1))
    return None


def sites_on_page(html):
    """Outbound business websites listed on an atlas state/theme page."""
    s = BeautifulSoup(html, "lxml")
    out = []
    for a in s.find_all("a", href=True):
        h = a["href"].strip()
        if not h.startswith("http"):
            continue
        host = urlparse(h).netloc.lower()
        if HOST in host or SKIP_HOSTS.search(h) or JUNK.search(h):
            continue
        out.append(h)
    return list(dict.fromkeys(out))


def load_existing():
    """Merge both the main harvest and any prior atlas output."""
    found = {}
    for name in ("leads_all.csv", "leads_atlas.csv"):
        p = BASE / name
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("domain"):
                        found[row["domain"]] = row
        except Exception:
            pass
    return found


def save(found):
    """
    Write the atlas's OWN pair of files.

    harvest_all.py owns leads.txt / leads_all.csv and rewrites them as it goes.
    If the atlas wrote those too, the two processes would race and one would
    clobber the other. Keeping separate files avoids that entirely; merge.py
    combines them afterwards.
    """
    with open(BASE / "leads_atlas.txt", "w", encoding="utf-8") as f:
        for d in sorted(found):
            f.write((found[d].get("site") or f"https://{d}") + "\n")
    with open(BASE / "leads_atlas.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "site", "name", "phone",
                                          "category", "source"])
        w.writeheader()
        for d in sorted(found):
            w.writerow(found[d])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="jewelry")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-states", type=int, default=0)
    args = ap.parse_args()

    themes = THEMES if args.all else [t.strip() for t in args.theme.split(",") if t.strip()]
    states = STATES[:args.max_states] if args.max_states else STATES

    found = load_existing()
    start = len(found)
    print(f"existing leads: {start}")
    print(f"themes: {themes}  states: {len(states)}\n")

    c = client()
    added = 0
    for theme in themes:
        for st in states:
            url = f"https://{HOST}/{theme}/{st}/"
            html = get(c, url)
            if not html:
                print(f"  {theme}/{st:<16} -")
                continue
            sites = sites_on_page(html)
            new = 0
            for s in sites:
                cs = clean_site(s)
                if not cs:
                    continue
                dom = urlparse(cs).netloc.lower()
                if dom in found:
                    continue
                found[dom] = {"domain": dom, "site": cs, "name": "",
                              "phone": "", "category": theme, "source": url}
                new += 1
            added += new
            print(f"  {theme}/{st:<16} {len(sites):>4} links, {new:>3} new "
                  f"(total {len(found)})", flush=True)
            time.sleep(0.6)
            if new:
                save(found)

    save(found)
    print(f"\ndone — +{added} new, {len(found)} total in leads.txt")


if __name__ == "__main__":
    main()
