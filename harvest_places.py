"""
harvest_places.py — dealer leads for the USA + Canada from the Google Places API.

Adapted from github.com/Neverklear/Business-Info-Collector (same idea:
category x city searches on Google Places), but:
  * one Text Search call returns the website directly (Places API "New"),
    so there are no per-business Place Details calls and no email scraping
  * a hard request budget, so the bill cannot run away
  * resumable, and skips every domain already in leads_*.csv
  * national chains and directory sites are dropped

Needs GOOGLE_MAPS_API_KEY in .env (Places API (New) enabled on the key).

  python harvest_places.py --dry-run            show the plan and cost, call nothing
  python harvest_places.py --budget 1000        run, stop after 1000 API calls
  python harvest_places.py --budget 1000 --country ca

Output: leads_places.csv / leads_places.txt (same 6 columns as the other
harvesters), so `python probe_forms.py leads_places.txt` works next.
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

OUT_CSV = BASE / "leads_places.csv"
OUT_TXT = BASE / "leads_places.txt"
DONE = BASE / "leads_places.done"

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ("places.displayName,places.websiteUri,places.nationalPhoneNumber,"
          "places.formattedAddress,nextPageToken")
COST_PER_1000 = 35.0   # Text Search (Enterprise fields), USD, before free tier

CATEGORIES = [
    "jewelry store", "estate jewelry", "watch dealer", "pre-owned watches",
    "antique store", "antique silver", "coin dealer", "gold and silver buyer",
    "pawn shop", "auction house", "antique mall", "vintage watch repair",
]

# Biggest markets first, so a small budget covers the most dealers.
CITIES = {
    "us": [
        "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Phoenix, AZ",
        "Philadelphia, PA", "San Antonio, TX", "San Diego, CA", "Dallas, TX", "Austin, TX",
        "Jacksonville, FL", "San Jose, CA", "Fort Worth, TX", "Columbus, OH", "Charlotte, NC",
        "Indianapolis, IN", "San Francisco, CA", "Seattle, WA", "Denver, CO", "Nashville, TN",
        "Washington, DC", "Oklahoma City, OK", "Boston, MA", "El Paso, TX", "Portland, OR",
        "Las Vegas, NV", "Detroit, MI", "Memphis, TN", "Louisville, KY", "Baltimore, MD",
        "Milwaukee, WI", "Albuquerque, NM", "Tucson, AZ", "Fresno, CA", "Sacramento, CA",
        "Kansas City, MO", "Atlanta, GA", "Miami, FL", "Raleigh, NC", "Omaha, NE",
        "Minneapolis, MN", "Tulsa, OK", "Tampa, FL", "New Orleans, LA", "Cleveland, OH",
        "Honolulu, HI", "Pittsburgh, PA", "Cincinnati, OH", "St. Louis, MO", "Orlando, FL",
        "Salt Lake City, UT", "Richmond, VA", "Birmingham, AL", "Buffalo, NY", "Hartford, CT",
        "Providence, RI", "Charleston, SC", "Savannah, GA", "Palm Beach, FL", "Fort Lauderdale, FL",
        "Scottsdale, AZ", "Santa Fe, NM", "Boise, ID", "Spokane, WA", "Des Moines, IA",
        "Madison, WI", "Knoxville, TN", "Greenville, SC", "Asheville, NC", "Lexington, KY",
        "Little Rock, AR", "Jackson, MS", "Baton Rouge, LA", "Albany, NY", "Rochester, NY",
        "Syracuse, NY", "Newark, NJ", "Princeton, NJ", "Wilmington, DE", "Annapolis, MD",
        "Alexandria, VA", "Norfolk, VA", "Columbia, SC", "Chattanooga, TN", "Grand Rapids, MI",
        "Ann Arbor, MI", "Toledo, OH", "Dayton, OH", "Akron, OH", "Fort Wayne, IN",
        "Wichita, KS", "Lincoln, NE", "Sioux Falls, SD", "Fargo, ND", "Billings, MT",
        "Anchorage, AK", "Reno, NV", "Santa Barbara, CA", "Palm Springs, CA", "Portland, ME",
        "Burlington, VT", "Manchester, NH", "New Haven, CT", "Greenwich, CT", "Naples, FL",
        "Sarasota, FL", "St. Petersburg, FL", "Jacksonville Beach, FL", "Mobile, AL", "Shreveport, LA",
        "Corpus Christi, TX", "Lubbock, TX", "Plano, TX", "Long Beach, CA", "Pasadena, CA",
        "Beverly Hills, CA", "Oakland, CA", "Tacoma, WA", "Eugene, OR", "Colorado Springs, CO",
    ],
    "ca": [
        "Toronto, ON", "Montreal, QC", "Vancouver, BC", "Calgary, AB", "Edmonton, AB",
        "Ottawa, ON", "Winnipeg, MB", "Quebec City, QC", "Hamilton, ON", "Kitchener, ON",
        "London, ON", "Victoria, BC", "Halifax, NS", "Oshawa, ON", "Windsor, ON",
        "Saskatoon, SK", "Regina, SK", "St. Catharines, ON", "Kelowna, BC", "Barrie, ON",
        "Kingston, ON", "Guelph, ON", "Mississauga, ON", "Markham, ON", "Oakville, ON",
        "Burlington, ON", "Niagara-on-the-Lake, ON", "St. John's, NL", "Fredericton, NB",
        "Moncton, NB", "Charlottetown, PE", "Sherbrooke, QC", "Nanaimo, BC", "Kamloops, BC",
    ],
}

# National chains, brand boutiques, marketplaces and directories: not prospects.
SKIP = re.compile(
    r"(kay\.com|zales|jared\.com|pandora|signetjewelers|tiffany|cartier|swarovski|"
    r"helzberg|peoplesjewellers|michaelhill|mappins|birks|banter|ernestjones|"
    r"brilliantearth|bluenile|macys|costco|walmart|target\.com|sears|kohls|"
    r"jcpenney|nordstrom|bloomingdales|saksfifthavenue|neimanmarcus|"
    r"rolex\.com|omegawatches|tagheuer|tourneau|watchesofswitzerland|mayors\.com|"
    r"bucherer|cashamerica|firstcash|ezpawn|valuepawn|pawnamerica|cashconverters|"
    r"goodwill|salvationarmy|savers|valuevillage|"
    r"facebook|instagram|yelp|linktr\.ee|google\.com|etsy|ebay|amazon|"
    r"yellowpages|tripadvisor|mapquest|bbb\.org)",
    re.I,
)


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
    rows = {}
    if OUT_CSV.exists():
        with open(OUT_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["domain"]] = r
    return rows


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
            f.write(rows[d]["site"] + "\n")


def search(client, key, query, region, token=None):
    body = {"textQuery": query, "pageSize": 20, "regionCode": region}
    if token:
        body["pageToken"] = token
    r = client.post(ENDPOINT, json=body, headers={
        "X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", choices=["us", "ca", "both"], default="both")
    ap.add_argument("--budget", type=int, default=1000,
                    help="max API calls this run (default 1000)")
    ap.add_argument("--pages", type=int, default=3, help="pages per search, 20 results each (max 3)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    countries = ["us", "ca"] if args.country == "both" else [args.country]
    plan = [(c, f"{cat} in {city}") for c in countries for city in CITIES[c]
            for cat in CATEGORIES]
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    todo = [(c, q) for c, q in plan if q not in done]
    max_calls = len(todo) * args.pages
    print(f"{len(plan)} searches planned ({len(CATEGORIES)} categories x cities), "
          f"{len(done)} already done, {len(todo)} to go")
    print(f"worst case {max_calls} calls (~${max_calls * COST_PER_1000 / 1000:.0f} before "
          f"Google's free tier); this run stops at {args.budget} calls "
          f"(~${args.budget * COST_PER_1000 / 1000:.0f})")
    if args.dry_run:
        return

    key = envload.get("GOOGLE_MAPS_API_KEY", "GOOGLE_PLACES_API_KEY")
    if not key:
        sys.exit("Add GOOGLE_MAPS_API_KEY=... to .env (Places API (New) enabled), then re-run.")

    known = load_known()
    rows = load_rows()
    calls = new = 0
    with httpx.Client(timeout=30) as client, open(DONE, "a", encoding="utf-8") as donef:
        for country, query in todo:
            token = None
            for page in range(args.pages):
                if calls >= args.budget:
                    break
                try:
                    data = search(client, key, query, country, token)
                except Exception as e:
                    print(f"  ! {query}: {e}")
                    if "HTTP 4" in str(e):
                        save(rows)
                        sys.exit("Stopping: the API refused the request (check key / billing).")
                    break
                calls += 1
                for p in data.get("places", []):
                    site = p.get("websiteUri") or ""
                    d = domain_of(site) if site else ""
                    if not d or d in known or d in rows or SKIP.search(d):
                        continue
                    rows[d] = dict(domain=d, site=f"https://{d}/",
                                   name=(p.get("displayName") or {}).get("text", ""),
                                   phone=p.get("nationalPhoneNumber", ""),
                                   category=query.split(" in ")[0],
                                   source=f"places-{country}")
                    new += 1
                token = data.get("nextPageToken")
                if not token:
                    break
                time.sleep(0.3)
            if calls >= args.budget:
                print(f"budget of {args.budget} calls reached")
                break
            donef.write(query + "\n")
            donef.flush()
            print(f"  {query:<48} total new leads: {new}   calls: {calls}", flush=True)
            if new and new % 50 < 20:
                save(rows)

    save(rows)
    print(f"done: {new} new leads this run, {len(rows)} in {OUT_TXT.name}, {calls} API calls")


if __name__ == "__main__":
    main()
