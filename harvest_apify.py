"""
harvest_apify.py — second-hand / vintage / antique JEWELLERY dealers (USA + Canada)
from Google Maps via Apify (compass/crawler-google-places).

Money rules:
  * only places WITH a website (one Apify filter; closed sites fail the free probe)
  * no paid add-ons (no details page, contacts, reviews, images)
  * every run carries maxTotalChargeUsd, so Apify itself stops at the budget
  * resumable: finished regions are recorded and never paid for twice

  python harvest_apify.py --dry-run
  python harvest_apify.py --budget-usd 20

Needs APIFY_TOKEN in .env.
Output: leads_apify.csv / leads_apify.txt, then:
  python probe_forms.py leads_apify.txt      (free: which have a form)
  python photo_score.py leads_apify          (free: rank by weak photos)
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from core import envload

ACTOR = "compass~crawler-google-places"
API = "https://api.apify.com/v2"
OUT_CSV = BASE / "leads_apify.csv"
OUT_TXT = BASE / "leads_apify.txt"
DONE = BASE / "leads_apify.done"

SEARCHES = [
    "estate jewelry", "antique jewelry", "vintage jewelry", "pre-owned jewelry",
    "jewelry buyer", "pre-owned watches", "antique dealer", "antique store",
]

REGIONS = [f"{s}, USA" for s in (
    "California", "Texas", "Florida", "New York", "Pennsylvania", "Illinois", "Ohio",
    "Georgia", "North Carolina", "Michigan", "New Jersey", "Virginia", "Washington",
    "Arizona", "Massachusetts", "Tennessee", "Indiana", "Maryland", "Missouri",
    "Wisconsin", "Colorado", "Minnesota", "South Carolina", "Alabama", "Louisiana",
    "Kentucky", "Oregon", "Oklahoma", "Connecticut", "Utah", "Iowa", "Nevada",
    "Arkansas", "Mississippi", "Kansas", "New Mexico", "Nebraska", "Idaho",
    "West Virginia", "Hawaii", "New Hampshire", "Maine", "Montana", "Rhode Island",
    "Delaware", "South Dakota", "North Dakota", "Alaska", "Vermont", "Wyoming",
    "District of Columbia")] + [f"{p}, Canada" for p in (
    "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba", "Saskatchewan",
    "Nova Scotia", "New Brunswick", "Newfoundland and Labrador", "Prince Edward Island")]

# Not prospects: thrift / charity, chains and brand boutiques, directories.
BAD_CATEGORY = re.compile(
    r"thrift|charity|donation|goodwill|salvation army|clothing|furniture store|"
    r"hardware|department store|shopping mall|discount store|dollar store|"
    r"costume jewelry|body piercing|tattoo|bead store|craft store", re.I)
BAD_DOMAIN = re.compile(
    r"(kay\.com|zales|jared\.com|pandora|signetjewelers|tiffany|cartier|swarovski|"
    r"helzberg|peoplesjewellers|michaelhill|mappins|birks|banter|brilliantearth|"
    r"bluenile|macys|costco|walmart|target\.com|sears|kohls|jcpenney|nordstrom|"
    r"rolex\.com|omegawatches|tourneau|watchesofswitzerland|mayors\.com|bucherer|"
    r"cashamerica|firstcash|ezpawn|valuepawn|pawnamerica|cashconverters|"
    r"goodwill|salvationarmy|savers|valuevillage|claires|"
    r"facebook|instagram|yelp|linktr\.ee|google\.|etsy|ebay|amazon|"
    r"yellowpages|tripadvisor|mapquest|bbb\.org|wixsite\.com/?$)", re.I)


def norm(h):
    h = (h or "").split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def domain_of(url):
    return norm(urlparse(url if "//" in url else "http://" + url).netloc)


def load_known():
    seen = set()
    for p in BASE.glob("leads_*.csv"):
        if p == OUT_CSV:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = (row.get("domain") or "").strip()
                    if d:
                        seen.add(norm(d))
        except Exception:
            pass
    return seen


def load_rows():
    if not OUT_CSV.exists():
        return {}
    with open(OUT_CSV, encoding="utf-8") as f:
        return {r["domain"]: r for r in csv.DictReader(f)}


def save(rows):
    cols = ["domain", "site", "name", "phone", "category", "source"]
    tmp = OUT_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in sorted(rows):
            w.writerow({k: rows[d].get(k, "") for k in cols})
    tmp.replace(OUT_CSV)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(rows[d]["site"] for d in sorted(rows)) + "\n")


def run_region(client, region, per_search, charge_cap):
    body = {
        "searchStringsArray": SEARCHES,
        "locationQuery": region,
        "maxCrawledPlacesPerSearch": per_search,
        "language": "en",
        "website": "withWebsite",
        "scrapePlaceDetailPage": False,
        "scrapeContacts": False,
        "maxReviews": 0,
        "maxImages": 0,
    }
    r = client.post(f"{API}/acts/{ACTOR}/runs",
                    params={"maxTotalChargeUsd": f"{charge_cap:.2f}"}, json=body)
    r.raise_for_status()
    run = r.json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        time.sleep(15)
        run = client.get(f"{API}/actor-runs/{run['id']}").json()["data"]
    items = client.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                       params={"clean": "true", "format": "json"}).json()
    cost = run.get("usageTotalUsd") or 0
    return run["status"], items, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-usd", type=float, default=20.0, help="total spend cap for this run")
    ap.add_argument("--per-search", type=int, default=150,
                    help="max places per search term per state/province")
    ap.add_argument("--only", help="run just this region, e.g. \"Connecticut, USA\"")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    todo = [r for r in REGIONS if r not in done]
    if args.only:
        todo = [args.only]
    worst = len(todo) * len(SEARCHES) * args.per_search
    print(f"{len(REGIONS)} regions x {len(SEARCHES)} searches, {len(done)} regions done, "
          f"{len(todo)} to go; up to {worst:,} places if every search filled up")
    print(f"this run stops at ${args.budget_usd:.2f} (Apify enforces it per run)")
    if args.dry_run:
        return

    tok = envload.get("APIFY_TOKEN", "APIFY_API_TOKEN")
    if not tok:
        sys.exit("Add APIFY_TOKEN=... to .env, then re-run.")

    known, rows = load_known(), load_rows()
    spent = 0.0
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {tok}"}) as client, \
            open(DONE, "a", encoding="utf-8") as donef:
        for region in todo:
            left = args.budget_usd - spent
            if left < 0.5:
                print("budget reached")
                break
            status, items, cost = run_region(client, region, args.per_search, left)
            spent += cost
            kept = dup = bad = 0
            for it in items:
                site = it.get("website") or ""
                d = domain_of(site) if site else ""
                cats = " ".join([it.get("categoryName") or ""] + (it.get("categories") or []))
                if not d:
                    continue
                if d in known or d in rows:
                    dup += 1
                    continue
                if BAD_DOMAIN.search(d) or BAD_CATEGORY.search(cats):
                    bad += 1
                    continue
                rows[d] = dict(domain=d, site=f"https://{d}/", name=it.get("title", ""),
                               phone=it.get("phone", ""), category=it.get("categoryName", ""),
                               source=f"apify-{region}")
                kept += 1
            save(rows)
            print(f"  {region:<32} {len(items):>4} places  +{kept} new  ({dup} dupes, "
                  f"{bad} off-niche)  ${cost:.2f}  total ${spent:.2f}", flush=True)
            if status == "SUCCEEDED":
                donef.write(region + "\n")
                donef.flush()
            else:
                print(f"  run ended {status} (budget cap or error); region will be retried")
                break
    print(f"done: {len(rows)} leads in {OUT_TXT.name}, spent ${spent:.2f} this run")


if __name__ == "__main__":
    main()
